from __future__ import annotations

from datetime import datetime, UTC

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
    create_bootstrap_run,
    process_next_bootstrap_batch,
    queue_bootstrap_run,
    request_bootstrap_control,
    resume_bootstrap_run,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.client_bootstrap_models import (
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    TenantRow,
)
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapBatchRow,
    PersonalContextBootstrapRunRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


OWNER_HUMAN_ID = "hid-bootstrap-owner"
OWNER_ACTOR_KEY = "owner-bootstrap"


def _seed_owner(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    human_id: str = OWNER_HUMAN_ID,
    actor_key: str = OWNER_ACTOR_KEY,
    external_actor_id: str = "bootstrap-owner-external",
) -> None:
    stamp = now_utc()
    if session.get(TenantRow, tenant_id) is None:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=f"bootstrap-{tenant_id[-8:]}",
                name=f"Bootstrap {tenant_id[-8:]}",
                status="ACTIVE",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    session.add(
        HumanIdentityRow(
            id=human_id,
            created_at=stamp,
        )
    )
    session.add(
        ClientTenantMembershipRow(
            id=new_id(),
            human_identity_id=human_id,
            tenant_id=tenant_id,
            role="OWNER",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id=external_actor_id,
        actor_key=actor_key,
        actor_category="owner",
        display_name="Nilvanda",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    session.flush()


class BootstrapHistoryAdapter:
    def __init__(self):
        self.chats = [
            {
                "external_thread_key": "rent-chat",
                "thread_type": "DIRECT",
                "title": "Ângelo",
            }
        ]
        self.messages = [
            {
                "source": "untrusted-payload-source",
                "source_account": "untrusted-payload-account",
                "source_message_id": f"rent-{index}",
                "external_sender_key": "angelo",
                "sender_display_name": "Ângelo",
                "sent_at": datetime(2026, 9, index, tzinfo=UTC),
                "text": f"Mensagem histórica {index}.",
                "type": "TEXT",
            }
            for index in range(1, 4)
        ]

    def list_chats(self):
        return self.chats

    def fetch_messages(self, chat_key, limit, cursor=None):
        assert chat_key == "rent-chat"
        offset = int(cursor or 0)
        page = self.messages[offset:offset + limit]
        next_cursor = (
            str(offset + len(page))
            if offset + len(page) < len(self.messages)
            else None
        )
        return {"messages": page, "next_cursor": next_cursor}


def _create_run(session, **overrides):
    payload = {
        "tenant_id": DEFAULT_TENANT_ID,
        "owner_human_identity_id": OWNER_HUMAN_ID,
        "represented_owner_actor_key": OWNER_ACTOR_KEY,
        "source_kind": "WHATSAPP_TEXT",
        "source_account": "primary",
        "source_revision": "export-2026-10-03",
        "source_selection": {"chat_keys": ["rent-chat"]},
        "consent_ref": "consent-bootstrap-001",
        "processing_budget": {
            "page_size": 1,
            "max_messages_per_chat": 10,
            "max_total_messages": 2,
        },
    }
    payload.update(overrides)
    return create_bootstrap_run(session, **payload)


def test_v2b_bootstrap_run_is_owner_scoped_and_idempotent(session):
    _seed_owner(session)

    first, created = _create_run(session)
    second, second_created = _create_run(session)

    assert created is True
    assert second_created is False
    assert second.id == first.id
    assert first.state == "CREATED"
    assert first.consent_ref == "consent-bootstrap-001"
    assert first.source_selection == {"chat_keys": ["rent-chat"]}
    assert first.processing_budget["max_total_messages"] == 2
    assert session.scalar(
        select(func.count()).select_from(PersonalContextBootstrapRunRow)
    ) == 1



def test_v2b_source_selection_order_does_not_change_run_identity(session):
    _seed_owner(session)
    first, created = _create_run(
        session,
        source_selection={"chat_keys": ["chat-b", "chat-a", "chat-b"]},
    )
    second, second_created = _create_run(
        session,
        source_selection={"chat_keys": ["chat-a", "chat-b"]},
    )

    assert created is True
    assert second_created is False
    assert second.id == first.id
    assert first.source_selection == {"chat_keys": ["chat-a", "chat-b"]}

def test_v2b_bootstrap_advances_in_bounded_resumable_batches(session):
    _seed_owner(session)
    run, _ = _create_run(session)
    queue_bootstrap_run(session, run.id)
    adapter = BootstrapHistoryAdapter()

    first = process_next_bootstrap_batch(
        session,
        run.id,
        adapter=adapter,
    )
    assert first.state == "QUEUED"
    assert first.resume_cursor == {
        "chat_index": 0,
        "message_cursor": "2",
    }
    assert first.metrics["total_messages_archived"] == 2
    assert session.get(PersonalContextBootstrapRunRow, run.id).progress[
        "batches_completed"
    ] == 1

    assert request_bootstrap_control(session, run.id, control="PAUSE").state == "PAUSED"
    assert resume_bootstrap_run(session, run.id).state == "QUEUED"

    second = process_next_bootstrap_batch(
        session,
        run.id,
        adapter=BootstrapHistoryAdapter(),
    )
    assert second.state == "COMPLETED"
    assert second.resume_cursor is None
    stored = session.get(PersonalContextBootstrapRunRow, run.id)
    assert stored.progress["batches_completed"] == 2
    assert stored.progress["total_messages_archived"] == 3
    assert stored.progress["total_messages_discovered"] == 3
    assert session.scalar(
        select(func.count()).select_from(PersonalContextBootstrapBatchRow)
    ) == 2
    assert session.scalar(
        select(func.count()).select_from(ConversationMessageRow)
    ) == 3
    archived = session.scalars(
        select(ConversationMessageRow).order_by(ConversationMessageRow.sent_at)
    ).all()
    assert {item.source for item in archived} == {"whatsapp"}
    assert {item.source_account for item in archived} == {"primary"}

    # Bootstrap ingestion is knowledge-only.
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(RelationshipRow)
    ) == 0



def test_v2b_historical_ingestion_stays_inside_run_tenant(session):
    tenant_b = "00000000-0000-4000-8000-000000000099"
    human_b = "hid-bootstrap-owner-b"
    actor_b = "owner-bootstrap-b"
    _seed_owner(
        session,
        tenant_id=tenant_b,
        human_id=human_b,
        actor_key=actor_b,
        external_actor_id="bootstrap-owner-b-external",
    )
    run, _ = _create_run(
        session,
        tenant_id=tenant_b,
        owner_human_identity_id=human_b,
        represented_owner_actor_key=actor_b,
        consent_ref="consent-bootstrap-tenant-b",
        source_account="tenant-b-account",
    )
    queue_bootstrap_run(session, run.id)

    first = process_next_bootstrap_batch(
        session,
        run.id,
        adapter=BootstrapHistoryAdapter(),
    )
    assert first.state == "QUEUED"

    archived = session.scalars(select(ConversationMessageRow)).all()
    assert archived
    assert {item.tenant_id for item in archived} == {tenant_b}
    assert {item.source_account for item in archived} == {"tenant-b-account"}
    assert session.scalar(
        select(func.count())
        .select_from(ConversationMessageRow)
        .where(ConversationMessageRow.tenant_id == DEFAULT_TENANT_ID)
    ) == 0

def test_v2b_pause_resume_and_cancel_apply_at_durable_boundaries(session):
    _seed_owner(session)
    run, _ = _create_run(session)
    queue_bootstrap_run(session, run.id)

    paused = request_bootstrap_control(
        session,
        run.id,
        control="PAUSE",
    )
    assert paused.state == "PAUSED"
    assert process_next_bootstrap_batch(
        session,
        run.id,
        adapter=BootstrapHistoryAdapter(),
    ).batch_id is None

    resumed = resume_bootstrap_run(session, run.id)
    assert resumed.state == "QUEUED"

    cancelled = request_bootstrap_control(
        session,
        run.id,
        control="CANCEL",
    )
    assert cancelled.state == "CANCELLED"
    assert process_next_bootstrap_batch(
        session,
        run.id,
        adapter=BootstrapHistoryAdapter(),
    ).state == "CANCELLED"
    assert session.scalar(
        select(func.count()).select_from(PersonalContextBootstrapBatchRow)
    ) == 0



def test_v2b_rejects_explicit_empty_source_selection(session):
    _seed_owner(session)
    with pytest.raises(
        PersonalContextBootstrapError,
        match="BOOTSTRAP_SOURCE_SELECTION_INVALID",
    ):
        _create_run(
            session,
            source_selection={"chat_keys": []},
        )


def test_v2b_fails_closed_when_represented_owner_is_ambiguous(session):
    _seed_owner(session)
    upsert_actor_binding(
        session,
        source="test-ambiguous",
        external_actor_id="bootstrap-owner-other",
        actor_key="owner-bootstrap-other",
        actor_category="owner",
        display_name="Other Owner",
        metadata={"owner": True},
    )

    with pytest.raises(
        PersonalContextBootstrapError,
        match="BOOTSTRAP_REPRESENTED_OWNER_AMBIGUOUS",
    ):
        _create_run(session)

def test_v2b_requires_active_owner_membership_and_owner_binding(session):
    stamp = now_utc()
    session.add(
        HumanIdentityRow(
            id="hid-non-owner",
            created_at=stamp,
        )
    )
    session.add(
        ClientTenantMembershipRow(
            id=new_id(),
            human_identity_id="hid-non-owner",
            tenant_id=DEFAULT_TENANT_ID,
            role="MEMBER",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    with pytest.raises(
        PersonalContextBootstrapError,
        match="BOOTSTRAP_OWNER_MEMBERSHIP_REQUIRED",
    ):
        create_bootstrap_run(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            owner_human_identity_id="hid-non-owner",
            represented_owner_actor_key="not-owner",
            source_kind="WHATSAPP_TEXT",
            consent_ref="consent",
        )


def test_v2b_failed_batch_rolls_back_partial_history_work(session):
    class FailingAdapter(BootstrapHistoryAdapter):
        def fetch_messages(self, chat_key, limit, cursor=None):
            if cursor is not None:
                raise RuntimeError("provider-page-failed")
            return super().fetch_messages(chat_key, limit, cursor)

    _seed_owner(session)
    run, _ = _create_run(
        session,
        processing_budget={
            "page_size": 1,
            "max_messages_per_chat": 10,
            "max_total_messages": 2,
        },
    )
    queue_bootstrap_run(session, run.id)

    result = process_next_bootstrap_batch(
        session,
        run.id,
        adapter=FailingAdapter(),
    )

    assert result.state == "FAILED"
    stored = session.get(PersonalContextBootstrapRunRow, run.id)
    assert "provider-page-failed" in stored.failure_summary
    batch = session.scalar(select(PersonalContextBootstrapBatchRow))
    assert batch.state == "FAILED"
    assert session.scalar(
        select(func.count()).select_from(ConversationMessageRow)
    ) == 0


def test_scan_limit_failure_never_completes_or_archives_partial_history(session):
    from attention_router.integrations.whatsapp_history import WhatsAppHistoryScanLimitExceeded

    class OversizedAdapter(BootstrapHistoryAdapter):
        def fetch_messages(self, chat_key, limit, cursor=None):
            raise WhatsAppHistoryScanLimitExceeded()

    _seed_owner(session)
    run, _ = _create_run(session)
    queue_bootstrap_run(session, run.id)
    result = process_next_bootstrap_batch(session, run.id, adapter=OversizedAdapter())
    assert result.state == "FAILED"
    assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(RelationshipRow)) == 0


def test_product_runtime_freezes_real_chat_selection_and_owner_controls(session):
    from types import SimpleNamespace

    from attention_router.application.personal_context_bootstrap_product import (
        PersonalContextBootstrapProductService,
    )
    from attention_router.config import Settings

    class ClientSessions:
        def authenticated_bootstrap(self, session, *, session_token):
            assert session_token == "session-token"
            return SimpleNamespace(
                active_tenant_id=DEFAULT_TENANT_ID,
                human_identity_id=OWNER_HUMAN_ID,
            )

    class Adapter:
        snapshot_limit = 3

        def list_chats(self):
            return [
                {
                    "external_thread_key": "chat-b",
                    "thread_type": "DIRECT",
                    "title": "B",
                },
                {
                    "external_thread_key": "chat-a",
                    "thread_type": "GROUP",
                    "title": "A",
                },
            ]

    _seed_owner(session)
    configured = Settings(
        _env_file=None,
        app_env="test",
        internal_ingress_hmac_secret="i" * 32,
        client_session_enabled=True,
        personal_context_bootstrap_enabled=True,
        personal_context_bootstrap_canary_tenant_id=DEFAULT_TENANT_ID,
        whatsapp_history_snapshot_limit=3,
    )
    service = PersonalContextBootstrapProductService(
        settings=configured,
        client_sessions=ClientSessions(),
        adapter=Adapter(),
    )

    row = service.create_and_queue(
        session,
        session_token="session-token",
        consent_ref="owner-consent-001",
    )

    assert row.state == "QUEUED"
    assert row.source_selection == {"chat_keys": ["chat-a", "chat-b"]}
    assert row.processing_budget == {
        "page_size": 3,
        "max_messages_per_chat": 3,
        "max_total_messages": 6,
    }
    service.adapter.max_scan_messages = 1000
    assert service._budget(selected_chat_count=1, processing_budget=None) == {
        "page_size": 3,
        "max_messages_per_chat": 1000,
        "max_total_messages": 500,
    }
    from attention_router.application.personal_context_bootstrap_product import (
        PersonalContextBootstrapProductTenantForbidden,
    )
    configured.personal_context_bootstrap_canary_tenant_id = None
    with pytest.raises(PersonalContextBootstrapProductTenantForbidden):
        service.status(session, session_token="session-token", run_id=row.id)
    configured.personal_context_bootstrap_canary_tenant_id = DEFAULT_TENANT_ID

    assert service.pause(
        session,
        session_token="session-token",
        run_id=row.id,
    ).state == "PAUSED"
    assert service.resume(
        session,
        session_token="session-token",
        run_id=row.id,
    ).state == "QUEUED"
    assert service.cancel(
        session,
        session_token="session-token",
        run_id=row.id,
    ).state == "CANCELLED"


def test_bootstrap_runtime_canary_advances_only_exact_tenant(session):
    from attention_router.application.personal_context_bootstrap_runtime import (
        run_personal_context_bootstrap_runtime_cycle,
    )

    tenant_b = "00000000-0000-4000-8000-000000000199"
    human_b = "hid-bootstrap-runtime-b"
    actor_b = "owner-bootstrap-runtime-b"

    _seed_owner(session)
    _seed_owner(
        session,
        tenant_id=tenant_b,
        human_id=human_b,
        actor_key=actor_b,
        external_actor_id="bootstrap-runtime-b-external",
    )
    run_a, _ = _create_run(session)
    run_b, _ = _create_run(
        session,
        tenant_id=tenant_b,
        owner_human_identity_id=human_b,
        represented_owner_actor_key=actor_b,
        consent_ref="consent-runtime-b",
    )
    queue_bootstrap_run(session, run_a.id)
    queue_bootstrap_run(session, run_b.id)

    result = run_personal_context_bootstrap_runtime_cycle(
        session,
        adapter=BootstrapHistoryAdapter(),
        run_limit=5,
        canary_tenant_id=DEFAULT_TENANT_ID,
    )

    assert result.runs_considered == 1
    assert result.batches_completed == 1
    assert result.runs_requeued == 1
    assert session.get(PersonalContextBootstrapRunRow, run_a.id).progress[
        "batches_completed"
    ] == 1
    assert session.get(PersonalContextBootstrapRunRow, run_b.id).progress == {}
