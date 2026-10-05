from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import pytest

from attention_router.application import services
from attention_router.application.memory import ArchivedMessageInput, archive_message, process_memory_ingestion_jobs
from attention_router.config import settings
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationParticipantRow,
    ConversationThreadRow,
    MemoryCandidateRow,
    MemoryClaimRow,
    MemoryEvidenceRow,
    MemoryIngestionJobRow,
)


pytestmark = pytest.mark.postgres


def test_postgres_incremental_memory_archive_job_claim_and_retry(Session, monkeypatch):
    monkeypatch.setattr(settings, "persistent_memory_enabled", True)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", True)
    payload = {
        "lineage_classification": "ORGANIC",
        "metadata": {"thread_key": "pg-memory-chat", "source_account": "pg-test"},
    }
    with Session() as session:
        first = services.receive_inbound_event(
            session, "pgtest", "memory-1", "message", "memory-actor", "Contato Sintético",
            "family_core", None, "Pode me chamar de André.", payload,
        )
        session.commit()
        second = services.receive_inbound_event(
            session, "pgtest", "memory-1", "message", "memory-actor", "Contato Sintético",
            "family_core", None, "Pode me chamar de André.", payload,
        )
        session.commit()
        assert first["id"] == second["id"]
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryIngestionJobRow)) == 1
        assert process_memory_ingestion_jobs(session) == 1
        session.commit()
        assert process_memory_ingestion_jobs(session) == 0
        assert session.scalar(select(func.count()).select_from(MemoryCandidateRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 1
        assert session.scalar(select(func.count()).select_from(MemoryEvidenceRow)) == 1


def test_postgres_archive_second_message_in_same_thread_keeps_utc_aware_order(Session):
    first_sent = datetime(
        2026,
        10,
        5,
        15,
        0,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    second_sent = datetime(2026, 10, 5, 18, 5, tzinfo=timezone.utc)

    with Session() as session:
        first, created = archive_message(
            session,
            ArchivedMessageInput(
                source="gmail",
                source_account="sha256:synthetic",
                thread_key="pg-aware-thread",
                thread_type="DIRECT",
                source_message_id="pg-aware-1",
                sender_key="sender@example.invalid",
                sender_display_name="Synthetic Sender",
                sent_at=first_sent,
                text="primeira",
                lineage_classification="ORGANIC",
            ),
        )
        assert created
        session.commit()
        thread_id = first.conversation_id

    with Session() as session:
        thread = session.get(ConversationThreadRow, thread_id)
        participant = session.scalar(
            select(ConversationParticipantRow).where(
                ConversationParticipantRow.conversation_id == thread_id,
                ConversationParticipantRow.external_participant_key
                == "sender@example.invalid",
            )
        )
        assert thread is not None
        assert participant is not None
        assert thread.first_message_at.utcoffset() is not None
        assert thread.last_message_at.utcoffset() is not None
        assert participant.last_seen_at.utcoffset() is not None

        second, created = archive_message(
            session,
            ArchivedMessageInput(
                source="gmail",
                source_account="sha256:synthetic",
                thread_key="pg-aware-thread",
                thread_type="DIRECT",
                source_message_id="pg-aware-2",
                sender_key="sender@example.invalid",
                sender_display_name="Synthetic Sender",
                sent_at=second_sent,
                text="segunda",
                lineage_classification="ORGANIC",
            ),
        )
        assert created
        assert second.sent_at == second_sent
        session.commit()

    with Session() as session:
        thread = session.get(ConversationThreadRow, thread_id)
        participant = session.scalar(
            select(ConversationParticipantRow).where(
                ConversationParticipantRow.conversation_id == thread_id,
                ConversationParticipantRow.external_participant_key
                == "sender@example.invalid",
            )
        )
        assert thread.first_message_at.astimezone(timezone.utc) == datetime(
            2026, 10, 5, 18, 0, tzinfo=timezone.utc
        )
        assert thread.last_message_at.astimezone(timezone.utc) == second_sent
        assert participant.last_seen_at.astimezone(timezone.utc) == second_sent
