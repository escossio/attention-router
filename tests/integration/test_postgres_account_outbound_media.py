"""PostgreSQL contract: unattributed account photo -> Artifact -> conversation.

Uses the disposable test DB, synthetic bytes and no external image provider.
"""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.artifact_understanding import (
    process_artifact_understandings,
    register_artifact_understanding_notification,
)
from attention_router.application.conversation_live_capture import (
    reconcile_live_capture_batch,
)
from attention_router.application.whatsapp_artifacts import (
    stage_whatsapp_artifact_notification,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow, ArtifactUnderstandingRow,
)
from attention_router.infrastructure.models import (
    ConversationMessageRow, ConversationThreadRow, ExecutionIntentRow,
    MemoryClaimRow, OutboxMessageRow,
)
from tests.integration.test_postgres_conversation_live_capture import STAMP, receipt
from tests.test_artifact_understanding import FakeProvider
from tests.test_whatsapp_outbound_artifacts import (
    _enable,
    _ready_notification,
)

pytestmark = pytest.mark.postgres


def _account_image():
    row = receipt("pg-account-unattributed-photo", content="", from_me=True)
    payload = deepcopy(row.payload)
    payload["event_origin"] = "UNKNOWN_FROM_ME"
    payload["owner_authenticated"] = False
    payload["actor_id"] = "peer-do-not-attribute@example.invalid"
    payload["metadata"].update({
        "message_type": "image",
        "has_media": True,
        "from_me": True,
        "from_me_classification": "UNKNOWN_FROM_ME",
        "final_from_me_classification": "UNKNOWN_FROM_ME",
        "from_me_classification_reason": "UNMATCHED_MEDIA_FROM_ME",
        "account_outbound_media_capture_basis": "VERIFIED_ACCOUNT_UNATTRIBUTED",
        "conversation_key": "wwebjs:peer@lid",
        "conversation_state": "READY",
        "source_account": "synthetic",
        "peer_id_kind": "lid",
        "outbound_provenance": None,
        "owner_self_chat": False,
        "authenticated_owner_self_chat_target": False,
        "is_group": False,
    })
    row.payload = payload
    return row


def test_pg_unattributed_owner_image_follows_same_source_while_hiding_ocr(
    Session, monkeypatch, tmp_path,
):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at",
        STAMP - timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "artifact_understanding_enabled", True)
    monkeypatch.setattr(settings, "artifact_understanding_provider", "openai")
    monkeypatch.setattr(settings, "artifact_understanding_model", "synthetic")
    monkeypatch.setattr(settings, "artifact_understanding_max_bytes", 1024)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)

    event = _account_image()
    with Session() as session:
        session.add(event)
        session.commit()

    with Session() as session:
        first = reconcile_live_capture_batch(session)
        session.commit()
        assert first.archived == 1
        assert first.linked_artifacts == 0
        row = session.scalar(select(ConversationMessageRow))
        assert row.message_type == "IMAGE"
        assert row.from_me is True
        assert row.sender_actor_id is None
        assert row.external_sender_key is None
        assert row.text is None
        assert row.metadata_json["attachment_state"] == "PENDING"
        original_message = row.id
        original_thread = row.conversation_id

    with Session() as session:
        notification, staging = _ready_notification(
            session, tmp_path, event_id=event.external_event_id,
        )
        canonical = stage_whatsapp_artifact_notification(
            session, notification, media_store=staging,
        )
        assert canonical is not None
        media_receipt = canonical.registration.receipt
        assert media_receipt.sender_actor_id is None
        semantic = register_artifact_understanding_notification(
            session, notification,
            artifact_id=canonical.registration.artifact.id,
        )
        assert semantic is not None
        assert semantic.status == "PENDING"
        session.commit()

    with Session() as session:
        fake_provider = FakeProvider()
        assert process_artifact_understandings(
            session, "postgres-synthetic-vision", provider=fake_provider,
        ) == 1
        session.commit()
        assert fake_provider.calls == 1

    with Session() as session:
        linked = reconcile_live_capture_batch(session)
        session.commit()
        assert linked.archived == 0
        assert linked.linked_artifacts == 1
        row = session.get(ConversationMessageRow, original_message)
        semantic = session.scalar(select(ArtifactUnderstandingRow))
        stored_receipt = session.scalar(select(ArtifactReceiptRow))
        assert row.id == original_message
        assert row.conversation_id == original_thread
        assert row.metadata_json["artifact_receipt_id"] == stored_receipt.id
        assert row.metadata_json["artifact_id"] == stored_receipt.artifact_id
        assert row.metadata_json["artifact_understanding_id"] == semantic.id
        assert row.metadata_json["artifact_understanding_status"] == "READY"
        assert row.metadata_json["source_inbound_event_id"] == event.id
        assert row.metadata_json["attachment_state"] == "READY"
        assert row.text is None
        assert row.sender_actor_id is None
        assert "R$ 42" not in str(row.metadata_json)
        assert "Documento de teste" not in str(row.metadata_json)
        assert session.scalar(select(func.count()).select_from(ConversationThreadRow)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
        assert reconcile_live_capture_batch(session).linked_artifacts == 0
