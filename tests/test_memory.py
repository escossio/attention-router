from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from attention_router.application.memory import (
    ArchivedMessageInput,
    HistoryBackfillService,
    archive_message,
    get_memory_evidence,
    ingest_message,
    memory_context,
    search_archive,
)
from attention_router.application import services
from attention_router.config import settings
from attention_router.infrastructure.models import ConversationMessageRow, MemoryCandidateRow, MemoryClaimRow, MemoryIngestionJobRow, QueueRow


def msg(session, text, key="actor-a", source_id=None, sent_at=None, thread="chat-a", thread_type="DIRECT"):
    row, created = archive_message(session, ArchivedMessageInput(
        source="synthetic", source_account="test", thread_key=thread, thread_type=thread_type,
        source_message_id=source_id or text, sender_key=key, sender_display_name="Observed",
        sent_at=sent_at or datetime.now(timezone.utc), text=text,
        lineage_classification="ORGANIC",
    ))
    assert created
    return row


def test_self_reported_name_promoted_with_provenance(session):
    row = msg(session, "Eu me chamo André.")
    claims = ingest_message(session, row.id)
    assert claims[0].predicate == "identity.self_reported_name"
    assert claims[0].object_text == "André"
    assert get_memory_evidence(session, claims[0].id)[0]["message_id"] == row.id


def test_transient_fact_is_archive_only(session):
    row = msg(session, "Hoje comi pizza.")
    ingest_message(session, row.id)
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0
    assert row.id in {item["message_id"] for item in search_archive(session, "pizza")}


def test_secret_is_redacted_and_not_searchable_or_promoted(session):
    row = msg(session, "Meu OTP é 123456.")
    assert row.text == "[REDACTED_SECRET]"
    assert row.searchable is False
    ingest_message(session, row.id)
    assert session.scalar(select(func.count()).select_from(MemoryCandidateRow)) == 0
    assert search_archive(session, "123456") == []


def test_temporal_supersession_and_context(session):
    first = msg(session, "Trabalho na Empresa A.", sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ingest_message(session, first.id)
    second = msg(session, "Trabalho na Empresa B.", sent_at=datetime(2026, 2, 1, tzinfo=timezone.utc), source_id="second")
    ingest_message(session, second.id)
    claims = session.scalars(select(MemoryClaimRow).order_by(MemoryClaimRow.created_at)).all()
    assert claims[0].status == "SUPERSEDED"
    assert claims[1].status == "ACTIVE"
    assert claims[1].supersedes_claim_id == claims[0].id
    assert memory_context(session, claims[1].subject_actor_id)["known_company"] == "Empresa B"


def test_group_keeps_real_sender_and_archive_backfill_is_idempotent(session):
    row = msg(session, "Minha irmã Ana vai falar com você.", key="joao", source_id="group-1", thread="family", thread_type="GROUP")
    assert row.sender_actor_id is not None
    ingest_message(session, row.id, mode="BACKFILL")
    assert ingest_message(session, row.id, mode="BACKFILL") == []
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1


class FakeHistoryAdapter:
    def __init__(self):
        self.chats = [
            {"external_thread_key": "known", "thread_type": "DIRECT", "title": "Known"},
            {"external_thread_key": "unknown", "thread_type": "DIRECT", "title": "Unknown"},
            {"external_thread_key": "family", "thread_type": "GROUP", "title": "Family"},
        ]
        self.messages = {
            "known": [{"source_message_id": "known-1", "external_sender_key": "actor-a", "sent_at": datetime(2026, 1, 1), "text": "Eu me chamo André.", "type": "TEXT"}],
            "unknown": [{"source_message_id": "unknown-1", "external_sender_key": "actor-b", "sent_at": datetime(2026, 1, 2), "text": "Servidor Dell e uma vaga de SRE.", "type": "TEXT"}],
            "family": [{"source_message_id": "family-1", "external_sender_key": "actor-c", "sent_at": datetime(2026, 1, 3), "text": "Minha irmã Ana vai falar com você.", "type": "TEXT"}],
        }

    def list_chats(self):
        return self.chats

    def fetch_messages(self, chat_key, limit, cursor=None):
        if cursor:
            return {"messages": [], "next_cursor": None}
        return {"messages": self.messages[chat_key][:limit], "next_cursor": None}


def test_backfill_dry_run_and_zero_live_pipeline(session):
    result = HistoryBackfillService(session, FakeHistoryAdapter()).run(dry_run=True, page_size=50)
    assert result.metrics["total_chats_discovered"] == 3
    assert result.metrics["total_messages_discovered"] == 3
    assert result.metrics["total_messages_archived"] == 3
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 0


def test_backfill_resume_and_second_run_are_idempotent(session):
    adapter = FakeHistoryAdapter()
    first = HistoryBackfillService(session, adapter).run(dry_run=False, chat_keys=["known", "family"], page_size=1)
    session.commit()
    assert first.metrics["total_messages_archived"] == 2
    second = HistoryBackfillService(session, adapter).run(dry_run=False, chat_keys=["known", "family"], page_size=1)
    assert second.metrics["messages_skipped"] == 2
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 2
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0


@pytest.mark.parametrize("lineage", ["SYNTHETIC", "HISTORICAL_UNKNOWN"])
def test_non_organic_lineage_is_archived_but_never_promoted(session, lineage):
    kwargs = {}
    if lineage == "SYNTHETIC":
        kwargs = {"scenario_id": "SCN-PE-001", "scenario_run_id": "run-memory"}
    row, created = archive_message(
        session,
        ArchivedMessageInput(
            source="synthetic",
            source_account="test",
            thread_key="lineage-test",
            thread_type="DIRECT",
            source_message_id=f"lineage-{lineage}",
            sender_key="synthetic-actor",
            sender_display_name="Synthetic actor",
            sent_at=datetime.now(timezone.utc),
            text="Eu me chamo Fixture.",
            lineage_classification=lineage,
            **kwargs,
        ),
    )
    assert created
    assert ingest_message(session, row.id) == []
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 0


def test_memory_context_is_actor_scoped(session):
    actor_a = msg(session, "Eu me chamo André.", key="actor-a", source_id="actor-a-name")
    ingest_message(session, actor_a.id)
    actor_b = msg(session, "Bom dia.", key="actor-b", source_id="actor-b-greeting")
    context_b = memory_context(session, actor_b.sender_actor_id)
    assert context_b["preferred_name"] is None
    assert all(fact["value"] != "André" for fact in context_b["relevant_current_facts"])


def test_semantic_duplicate_memory_claim_keeps_one_active_claim_and_multiple_evidence(session):
    first = msg(session, "Pode me chamar de Morgan", key="actor-dedupe", source_id="dedupe-1")
    second = msg(session, "Me chama de Morgan.", key="actor-dedupe", source_id="dedupe-2")
    ingest_message(session, first.id)
    ingest_message(session, second.id)
    claims = session.scalars(select(MemoryClaimRow).where(MemoryClaimRow.predicate == "identity.preferred_name")).all()
    assert len(claims) == 1
    assert claims[0].status == "ACTIVE"
    assert len(get_memory_evidence(session, claims[0].id)) == 2


def test_backfill_resume_cursor_completes_next_page(session):
    class ResumableAdapter(FakeHistoryAdapter):
        def __init__(self):
            super().__init__()
            self.messages["known"].append({"source_message_id": "known-2", "external_sender_key": "actor-a", "sent_at": datetime(2026, 1, 4), "text": "Trabalho na Empresa X.", "type": "TEXT"})

        def fetch_messages(self, chat_key, limit, cursor=None):
            messages = self.messages[chat_key]
            offset = int(cursor or 0)
            page = messages[offset:offset + limit]
            next_cursor = str(offset + len(page)) if offset + len(page) < len(messages) else None
            return {"messages": page, "next_cursor": next_cursor}

    adapter = ResumableAdapter()
    first = HistoryBackfillService(session, adapter).run(dry_run=False, chat_keys=["known"], page_size=1, max_messages_per_chat=1)
    assert first.resume_cursor == {"chat_index": 0, "message_cursor": "1"}
    session.commit()
    second = HistoryBackfillService(session, adapter).run(dry_run=False, chat_keys=["known"], page_size=1, max_messages_per_chat=1, resume_cursor=first.resume_cursor)
    assert second.resume_cursor is None
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 2


def test_incremental_entrypoint_archives_and_enqueues_once(session, monkeypatch):
    monkeypatch.setattr(settings, "persistent_memory_enabled", True)
    monkeypatch.setattr(settings, "memory_ingestion_enabled", True)
    payload = {
        "lineage_classification": "ORGANIC",
        "metadata": {"thread_key": "live-chat", "source_account": "test"},
    }
    first = services.receive_inbound_event(
        session, "synthetic", "evt-1", "message", "actor-live", "Observed", "unknown", None,
        "Pode me chamar de André.", payload,
    )
    session.commit()
    second = services.receive_inbound_event(
        session, "synthetic", "evt-1", "message", "actor-live", "Observed", "unknown", None,
        "Pode me chamar de André.", payload,
    )
    assert first["id"] == second["id"]
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
    assert session.scalar(select(func.count()).select_from(MemoryCandidateRow)) == 0
    job = session.scalar(select(MemoryIngestionJobRow))
    assert job.status == "PENDING"
    from attention_router.application.memory import process_memory_ingestion_jobs
    assert process_memory_ingestion_jobs(session) == 1
    assert process_memory_ingestion_jobs(session) == 0
    assert session.scalar(select(func.count()).select_from(MemoryClaimRow)) == 1


def test_from_me_is_archive_only_and_never_decision_input(session, monkeypatch):
    monkeypatch.setattr(settings, "persistent_memory_enabled", True)
    payload = {"metadata": {"thread_key": "chat", "from_me": True}}
    services.receive_inbound_event(
        session, "wwebjs", "out-1", "message", "actor-a", "Andy", "unknown", None,
        "Entendi.", payload,
    )
    row = session.scalar(select(ConversationMessageRow))
    assert row.from_me is True
    assert row.direction == "OUTBOUND"
    assert session.scalar(select(func.count()).select_from(QueueRow)) == 0


def test_memory_archive_failure_does_not_fail_inbound(session, monkeypatch):
    monkeypatch.setattr(settings, "persistent_memory_enabled", True)
    def fail(*args, **kwargs):
        raise RuntimeError("extractor unavailable")
    monkeypatch.setattr(services, "archive_incremental_message", fail)
    result = services.receive_inbound_event(
        session, "synthetic", "evt-fail", "message", "actor-fail", "Observed", "unknown", None,
        "Bom dia.", {"metadata": {"thread_key": "chat"}},
    )
    assert result["id"]
