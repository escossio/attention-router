"""Disposable PostgreSQL certification for the archive-only WhatsApp frontier."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.conversation_live_capture import (
    capture_live_receipt,
    reconcile_live_capture_batch,
)
from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationThreadRow,
    InboundEventRow,
    MemoryCandidateRow,
    MemoryIngestionJobRow,
    VoiceTranscriptionRow,
)

pytestmark = pytest.mark.postgres
STAMP = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def receipt(name, *, content="Mensagem de teste", from_me=False, voice=False):
    origin = "OWNER_MANUAL_OUTBOUND_OBSERVED" if from_me else "EXTERNAL_INBOUND"
    payload = {
        "source": "wwebjs",
        "channel": "whatsapp",
        "event_type": "message",
        "event_origin": origin,
        "owner_authenticated": False,
        "actor_id": "person@example.invalid",
        "content": "" if voice else content,
        "occurred_at": (STAMP - timedelta(seconds=5)).isoformat(),
        "metadata": {
            "from_me": from_me,
            "from_me_classification": origin if from_me else None,
            "message_type": "ptt" if voice else "chat",
            "has_media": voice,
            "conversation_key": "wwebjs:contact@c.us",
            "conversation_state": "READY",
            "source_account": "synthetic",
        },
    }
    return InboundEventRow(
        id=new_id(),tenant_id=DEFAULT_TENANT_ID,source="wwebjs",
        external_event_id=name,event_type="message",payload=payload,
        payload_hash=stable_hash(payload),received_at=STAMP,
        processed_at=STAMP,interaction_id=None,status="PROCESSED",
        error=None,correlation_id=new_id(),lineage_classification="ORGANIC",
    )


def test_postgres_live_archive_same_thread_stt_and_reconciliation(
    Session, monkeypatch,
):
    monkeypatch.setattr(settings, "conversation_capture_enabled", True)
    monkeypatch.setattr(
        settings, "conversation_capture_start_at", STAMP-timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "conversation_capture_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "persistent_memory_enabled", False)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", False)

    with Session() as session:
        incoming = receipt("pg-live-incoming")
        owner = receipt("pg-live-owner",from_me=True,content="Resposta de teste")
        voice = receipt("pg-live-voice",voice=True)
        session.add_all([incoming,owner,voice])
        session.flush()
        assert capture_live_receipt(session,incoming).status=="ARCHIVED"
        assert capture_live_receipt(session,owner).status=="ARCHIVED"
        session.commit()

    with Session() as session:
        summary=reconcile_live_capture_batch(session,limit=10)
        assert summary.examined==1 and summary.archived==1
        transcript=VoiceTranscriptionRow(
            id=new_id(),tenant_id=DEFAULT_TENANT_ID,inbound_event_id=voice.id,
            media_artifact_id=None,status="READY",transcript_text="Pretão é um gato.",
            provider="synthetic",model="synthetic",provider_request_reference=None,
            error_code=None,attempt_count=1,claimed_at=None,claimed_by=None,
            created_at=STAMP,updated_at=STAMP,completed_at=STAMP,
        )
        session.add(transcript)
        session.flush()
        # Exercise the PostgreSQL JSONB join for an archive that was already
        # created before STT completed, but missed the original callback.
        recovered_transcript = reconcile_live_capture_batch(session, limit=10)
        assert recovered_transcript.archived == 0
        assert recovered_transcript.attached_transcripts == 1
        session.commit()

    with Session() as session:
        rows=session.scalars(select(ConversationMessageRow).order_by(
            ConversationMessageRow.source_message_id,
        )).all()
        assert len(rows)==3
        assert {r.source_message_id for r in rows}=={
            "pg-live-owner","pg-live-incoming","pg-live-voice",
        }
        assert len({r.conversation_id for r in rows})==1
        assert session.scalar(select(func.count()).select_from(ConversationThreadRow))==1
        assert next(r for r in rows if r.message_type=="VOICE").text=="Pretão é um gato."
        assert next(r for r in rows if r.from_me).direction=="OUTBOUND"
        assert all(r.sent_at.utcoffset() is not None for r in rows)
        assert all(r.metadata_json["source_received_at"] for r in rows)
        assert session.scalar(select(func.count()).select_from(MemoryIngestionJobRow))==0
        assert session.scalar(select(func.count()).select_from(MemoryCandidateRow))==0
        assert reconcile_live_capture_batch(session).examined==0
