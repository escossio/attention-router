"""PostgreSQL proof for Artifact Understanding V1."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from attention_router.application.artifact_understanding import (
    ArtifactUnderstandingProviderResult,
    ArtifactUnderstandingResult,
    process_artifact_understandings,
    register_artifact_understanding_notification,
)
from attention_router.application.platform.artifact_storage import (
    ArtifactStageInput,
    stage_artifact_receipt,
)
from attention_router.application.voice_media import MediaReadyNotification
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id
from attention_router.infrastructure.artifact_models import ArtifactUnderstandingRow
from attention_router.infrastructure.artifact_store import LocalArtifactStore
from attention_router.infrastructure.models import (
    InboundEventRow,
    InteractionRow,
    QueueRow,
)
from attention_router.infrastructure.repository import create_inbound_event


pytestmark = pytest.mark.postgres
STAMP = datetime(2026, 9, 22, 1, 30, tzinfo=UTC)


class FakeProvider:
    provider_name = "fake"
    model = "gpt-5.6-sol"

    def analyze(self, data, *, mime_type, filename):
        assert data == b"%PDF postgres understanding"
        assert mime_type == "application/pdf"
        return ArtifactUnderstandingProviderResult(
            analysis=ArtifactUnderstandingResult(
                summary="PDF PostgreSQL compreendido.",
                extracted_text="VALOR 99",
                visual_description="",
                key_facts=["valor 99"],
                language="pt-BR",
                text_truncated=False,
            ),
            provider=self.provider_name,
            model=self.model,
            request_reference="pg-fake-request",
        )


def test_postgres_artifact_understanding_waits_processes_and_releases(
    Session,
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "artifacts"
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(root))
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "artifact_understanding_enabled", True)
    monkeypatch.setattr(settings, "artifact_understanding_provider", "openai")
    monkeypatch.setattr(settings, "artifact_understanding_model", "gpt-5.6-sol")
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024 * 1024)

    with Session() as session:
        interaction = InteractionRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            event_type="message",
            contact_id="synthetic-contact",
            contact_name="Synthetic",
            relationship_category="unknown",
            inbound_text="",
            state="RECEIVED",
            correlation_id=new_id(),
            created_at=STAMP,
            updated_at=STAMP,
        )
        session.add(interaction)
        session.flush()
        event = create_inbound_event(
            session,
            source="wwebjs",
            external_event_id="wamid.pg.understanding",
            event_type="message",
            payload={
                "actor_id": "synthetic-contact",
                "channel": "whatsapp",
                "content": "",
                "metadata": {
                    "source_account": "whatsapp-local",
                    "message_type": "document",
                    "has_media": True,
                },
            },
            correlation_id=interaction.correlation_id,
            tenant_id=DEFAULT_TENANT_ID,
            received_at=STAMP,
        )
        event.interaction_id = interaction.id
        session.add(
            QueueRow(
                id=f"decision:{event.id}",
                kind="decision",
                payload={
                    "event_id": event.id,
                    "interaction_id": interaction.id,
                },
                status="PENDING",
                created_at=STAMP,
                processed_at=None,
            )
        )
        store = LocalArtifactStore(root, max_bytes=1024 * 1024)
        staged = stage_artifact_receipt(
            session,
            store,
            ArtifactStageInput(
                tenant_id=DEFAULT_TENANT_ID,
                data=b"%PDF postgres understanding",
                artifact_kind="DOCUMENT",
                mime_type="application/pdf",
                source_channel="channel.whatsapp",
                external_receipt_id=(
                    "whatsapp:wamid.pg.understanding:media"
                ),
                received_at=STAMP,
                source_account="whatsapp-local",
                original_filename="proof.pdf",
            ),
        )
        understanding = register_artifact_understanding_notification(
            session,
            MediaReadyNotification(
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_event_id="wamid.pg.understanding",
                media_ref=staged.stored.storage_reference,
                content_sha256=staged.stored.content_sha256,
                mime_type="application/pdf",
                size_bytes=staged.stored.size_bytes,
                media_kind="document",
                original_filename="proof.pdf",
                capture_status="READY",
            ),
            artifact_id=staged.registration.artifact.id,
        )
        session.commit()
        understanding_id = understanding.id
        event_id = event.id
        queue_id = f"decision:{event.id}"

    with Session() as session:
        row = session.get(ArtifactUnderstandingRow, understanding_id)
        queue = session.get(QueueRow, queue_id)
        assert row.status == "PENDING"
        assert queue.status == "WAITING_ARTIFACT_UNDERSTANDING"

        assert process_artifact_understandings(
            session,
            "postgres-worker",
            provider=FakeProvider(),
        ) == 1

    with Session() as session:
        row = session.get(ArtifactUnderstandingRow, understanding_id)
        event = session.get(InboundEventRow, event_id)
        queue = session.get(QueueRow, queue_id)
        assert row.status == "READY"
        assert row.summary_text == "PDF PostgreSQL compreendido."
        assert row.extracted_text == "VALOR 99"
        assert row.artifact_id is not None
        assert event is not None
        assert queue.status == "PENDING"
        assert session.scalar(
            select(ArtifactUnderstandingRow.id).where(
                ArtifactUnderstandingRow.inbound_event_id == event_id
            )
        ) == understanding_id
