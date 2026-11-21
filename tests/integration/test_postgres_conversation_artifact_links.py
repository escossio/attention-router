"""Disposable PostgreSQL proof for live conversation to Artifact continuity.

All fixture values are synthetic; provider and image processing are never called.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.conversation_live_capture import (
    reconcile_live_capture_batch,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.artifact_models import (
    ArtifactReceiptRow,
    ArtifactRow,
    ArtifactUnderstandingRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationThreadRow,
    MemoryIngestionJobRow,
    ResourceRow,
)
from tests.integration.test_postgres_conversation_live_capture import STAMP, receipt


pytestmark = pytest.mark.postgres


def _photo(name, *, at=STAMP):
    item = receipt(name)
    payload = dict(item.payload)
    metadata = dict(payload["metadata"])
    metadata["message_type"] = "image"
    metadata["has_media"] = True
    payload["metadata"] = metadata
    payload["content"] = "Legenda original sintética"
    item.payload = payload
    item.received_at = at
    item.payload_hash = stable_hash(payload)
    return item


def _artifact(session, event, *, state="READY"):
    stamp = now_utc()
    resource = ResourceRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID, resource_type="ARTIFACT",
        canonical_name="artifact-synthetic-" + new_id(), status="ACTIVE",
        metadata_json={}, created_at=stamp, updated_at=stamp,
    )
    session.add(resource)
    session.flush()
    art = ArtifactRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID,
        resource_id=resource.id, content_sha256="a"*64, artifact_kind="IMAGE",
        mime_type="image/png", size_bytes=42, storage_provider="local-fs-v1",
        storage_reference="sha256:" + "a"*64, status="AVAILABLE",
        metadata_json={}, created_at=stamp, updated_at=stamp,
    )
    session.add(art)
    session.flush()
    receipt_row = ArtifactReceiptRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID, artifact_id=art.id,
        source_channel="channel.whatsapp", source_account="synthetic",
        external_receipt_id=f"whatsapp:{event.external_event_id}:media",
        sender_actor_id=event.payload["actor_id"],
        received_at=event.received_at, metadata_json={}, created_at=stamp,
    )
    understanding = ArtifactUnderstandingRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID,
        artifact_id=art.id, inbound_event_id=event.id,
        status=state, content_kind="IMAGE",
        summary_text="OCR SECRETO SINTÉTICO" if state=="READY" else None,
        extracted_text="OCR SECRETO SINTÉTICO" if state=="READY" else None,
        visual_description="Foto de teste" if state=="READY" else None,
        key_facts_json=["prova sintética"] if state=="READY" else [],
        language="pt-BR" if state=="READY" else None,
        text_truncated=False,
        provider="synthetic", model="fake", prompt_version="synthetic",
        attempt_count=1, claimed_at=None, claimed_by=None,
        created_at=stamp, updated_at=stamp,
        completed_at=stamp if state=="READY" else None,
    )
    session.add_all([receipt_row, understanding])
    session.flush()
    return art, receipt_row, understanding


def test_pg_photo_event_then_semantic_ready_updates_same_archived_message(
    Session, monkeypatch,
):
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at", STAMP - timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)

    event = _photo("pg-image-new")
    with Session() as session:
        session.add(event)
        session.commit()

    with Session() as session:
        initial = reconcile_live_capture_batch(session)
        session.commit()
        assert initial.archived == 1
        assert initial.linked_artifacts == 0
        row = session.scalar(select(ConversationMessageRow))
        row_identity = (row.id, row.conversation_id, row.sent_at)
        assert row.message_type == "IMAGE"
        assert row.text == "Legenda original sintética"
        assert row.metadata_json["attachment_state"] == "PENDING"

    with Session() as session:
        existing_event = session.get(type(event), event.id)
        artifact, receipt_row, understanding = _artifact(session, existing_event)
        session.commit()

    with Session() as session:
        result = reconcile_live_capture_batch(session)
        session.commit()
        assert result.archived == 0
        assert result.linked_artifacts == 1
        row = session.scalar(select(ConversationMessageRow))
        assert (row.id, row.conversation_id, row.sent_at) == row_identity
        assert row.metadata_json["artifact_id"] == artifact.id
        assert row.metadata_json["artifact_receipt_id"] == receipt_row.id
        assert row.metadata_json["artifact_understanding_id"] == understanding.id
        assert row.metadata_json["artifact_understanding_status"] == "READY"
        assert row.metadata_json["source_inbound_event_id"] == event.id
        assert "OCR SECRETO" not in str(row.metadata_json)
        assert "OCR SECRETO" not in row.text
        assert session.scalar(select(func.count()).select_from(ConversationThreadRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 0
        assert reconcile_live_capture_batch(session).linked_artifacts == 0


def test_pg_artifact_link_reconciler_recovers_late_visual_understanding(
    Session, monkeypatch,
):
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at", STAMP - timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)

    event = _photo("pg-image-async")
    with Session() as session:
        session.add(event)
        artifact, receipt_row, understanding = _artifact(session, event, state="PENDING")
        session.commit()

    with Session() as session:
        result = reconcile_live_capture_batch(session)
        session.commit()
        assert result.archived == 1
        assert result.linked_artifacts == 1
        message = session.scalar(select(ConversationMessageRow))
        assert message.metadata_json["attachment_state"] == "READY"
        assert message.metadata_json["artifact_understanding_status"] == "PENDING"
        record_id = message.id

    with Session() as session:
        semantic = session.get(ArtifactUnderstandingRow, understanding.id)
        semantic.status = "READY"
        semantic.summary_text = "OCR SECRETO SINTÉTICO"
        semantic.extracted_text = "OCR SECRETO SINTÉTICO"
        semantic.visual_description = "Foto de teste"
        semantic.completed_at = now_utc()
        session.commit()

    with Session() as session:
        result = reconcile_live_capture_batch(session)
        session.commit()
        assert result.linked_artifacts == 1
        message = session.get(ConversationMessageRow, record_id)
        assert message.metadata_json["artifact_understanding_id"] == understanding.id
        assert message.metadata_json["artifact_understanding_status"] == "READY"
        assert message.metadata_json["artifact_id"] == artifact.id
        assert message.metadata_json["artifact_receipt_id"] == receipt_row.id
        assert "OCR SECRETO" not in str(message.metadata_json)
        assert message.text == "Legenda original sintética"
        assert reconcile_live_capture_batch(session).linked_artifacts == 0
