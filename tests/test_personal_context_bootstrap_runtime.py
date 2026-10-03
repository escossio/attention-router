from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from attention_router.application.personal_context_bootstrap import (
    create_bootstrap_run,
    queue_bootstrap_run,
)
from attention_router.application.personal_context_bootstrap_runtime import (
    PersonalContextBootstrapRuntimeResult,
    run_personal_context_bootstrap_cycle,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import worker
from attention_router.infrastructure.client_bootstrap_models import (
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ConversationMessageRow,
    MemoryClaimRow,
    TenantRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


HUMAN = "hid-bootstrap-runtime"
OWNER = "owner-bootstrap-runtime"


def _owner(session, tenant_id=DEFAULT_TENANT_ID):
    stamp = now_utc()
    if session.get(TenantRow, tenant_id) is None:
        session.add(
            TenantRow(
                id=tenant_id,
                slug=f"bootstrap-runtime-{tenant_id[-6:]}",
                name="Bootstrap Runtime",
                status="ACTIVE",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    if session.get(HumanIdentityRow, HUMAN) is None:
        session.add(HumanIdentityRow(id=HUMAN, created_at=stamp))
    if session.scalar(
        select(ClientTenantMembershipRow).where(
            ClientTenantMembershipRow.human_identity_id == HUMAN,
            ClientTenantMembershipRow.tenant_id == tenant_id,
        )
    ) is None:
        session.add(
            ClientTenantMembershipRow(
                id=new_id(),
                human_identity_id=HUMAN,
                tenant_id=tenant_id,
                role="OWNER",
                status="ACTIVE",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    upsert_actor_binding(
        session,
        source="wwebjs",
        external_actor_id=f"{tenant_id}-owner@c.us",
        actor_key=OWNER,
        actor_category="owner",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )
    session.flush()


class Adapter:
    def list_chats(self):
        return [
            {
                "external_thread_key": "chat-a",
                "thread_type": "DIRECT",
                "title": "Pessoa",
            }
        ]

    def fetch_messages(self, chat_key, limit, cursor=None):
        messages = [
            {
                "source_message_id": "historical-1",
                "external_sender_key": "person@c.us",
                "sent_at": datetime(2026, 9, 1, tzinfo=UTC),
                "text": "Eu me chamo Pessoa.",
                "type": "TEXT",
                "from_me": False,
                "metadata": {},
            }
        ]
        return {"messages": messages if cursor is None else [], "next_cursor": None}


def _run(session, tenant_id=DEFAULT_TENANT_ID, source_account="default"):
    _owner(session, tenant_id)
    row, _ = create_bootstrap_run(
        session,
        tenant_id=tenant_id,
        owner_human_identity_id=HUMAN,
        represented_owner_actor_key=OWNER,
        source_kind="WHATSAPP_TEXT",
        source_account=source_account,
        consent_ref=f"consent-{tenant_id}",
        source_selection={"chat_keys": ["chat-a"]},
        processing_budget={
            "page_size": 10,
            "max_messages_per_chat": 10,
            "max_total_messages": 10,
        },
    )
    queue_bootstrap_run(session, row.id)
    return row


def test_runtime_advances_only_canary_queued_run_and_preserves_historical_lineage(
    session,
):
    run = _run(session)
    result = run_personal_context_bootstrap_cycle(
        session,
        adapter=Adapter(),
        source_account="default",
        run_limit=2,
        canary_tenant_id=DEFAULT_TENANT_ID,
    )

    assert result.runs_considered == 1
    assert result.runs_completed == 1
    assert result.messages_archived == 1
    message = session.scalar(select(ConversationMessageRow))
    assert message is not None
    assert (
        message.metadata_json["platform_lineage"]["classification"]
        == "HISTORICAL_UNKNOWN"
    )
    assert session.scalar(
        select(func.count()).select_from(MemoryClaimRow)
    ) == 0
    session.refresh(run)
    assert run.state == "COMPLETED"


def test_runtime_skips_other_tenant_and_source_account(session):
    tenant_b = "00000000-0000-4000-8000-000000000256"
    _run(session, tenant_id=tenant_b, source_account="other")

    result = run_personal_context_bootstrap_cycle(
        session,
        adapter=Adapter(),
        source_account="default",
        canary_tenant_id=DEFAULT_TENANT_ID,
    )
    assert result.runs_considered == 0
    assert session.scalar(
        select(func.count()).select_from(ConversationMessageRow)
    ) == 0


def test_bootstrap_runtime_settings_default_off_and_require_secret_when_enabled():
    configured = Settings(_env_file=None)
    assert configured.personal_context_bootstrap_runtime_enabled is False
    assert configured.personal_context_bootstrap_runtime_interval_seconds == 30
    assert configured.personal_context_bootstrap_runtime_run_limit == 2
    assert configured.personal_context_bootstrap_runtime_canary_tenant_id is None

    try:
        Settings(
            _env_file=None,
            personal_context_bootstrap_runtime_enabled=True,
            whatsapp_history_hmac_secret=None,
        )
    except ValueError as error:
        assert "WHATSAPP_HISTORY_HMAC_SECRET" in str(error)
    else:
        raise AssertionError("runtime must require history HMAC secret")


def test_worker_bootstrap_runtime_schedule_respects_flag_and_interval(
    session,
    monkeypatch,
):
    calls = []

    def fake_cycle(*args, **kwargs):
        calls.append(kwargs)
        return PersonalContextBootstrapRuntimeResult(runs_considered=1)

    monkeypatch.setattr(
        worker,
        "run_personal_context_bootstrap_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_runtime_enabled",
        False,
    )

    result, last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is None
    assert last is None

    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_runtime_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "whatsapp_history_hmac_secret",
        "test-secret",
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_runtime_interval_seconds",
        30,
    )
    result, last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is not None
    assert last == 100.0
    assert len(calls) == 1

    result, same = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=110.0,
        last_run_monotonic=last,
    )
    assert result is None
    assert same == last
    assert len(calls) == 1
