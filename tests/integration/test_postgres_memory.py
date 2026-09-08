from sqlalchemy import func, select

import pytest

from attention_router.application import services
from attention_router.application.memory import process_memory_ingestion_jobs
from attention_router.config import settings
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    MemoryCandidateRow,
    MemoryClaimRow,
    MemoryEvidenceRow,
    MemoryIngestionJobRow,
)


pytestmark = pytest.mark.postgres


def test_postgres_incremental_memory_archive_job_claim_and_retry(Session, monkeypatch):
    monkeypatch.setattr(settings, "persistent_memory_enabled", True)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", True)
    payload = {"metadata": {"thread_key": "pg-memory-chat", "source_account": "pg-test"}}
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
