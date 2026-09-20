from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import services
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
    PersonalContextRuntimeCycleResult,
    run_personal_context_runtime_cycle,
)
from attention_router.application.platform.capability_pack import (
    provision_internal_providers,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import worker
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    ExecutionIntentRow,
    InteractionRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
    TenantRow,
    TimelineEventRow,
)


ACTOR = "owner-v1g"
EXTERNAL = "owner-v1g@c.us"


def _ensure_tenant(session):
    if session.get(TenantRow, DEFAULT_TENANT_ID) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=DEFAULT_TENANT_ID,
            slug=DEFAULT_TENANT_ID,
            name="default",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _owner(
    session,
    *,
    external: str = EXTERNAL,
    primary: bool = True,
):
    _ensure_tenant(session)
    metadata = {"owner": True}
    if primary:
        metadata["owner_channel_role"] = "PRIMARY_OWNER_WHATSAPP"
    row = ActorBindingRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_actor_id=external,
        actor_key=ACTOR,
        display_name="Owner",
        actor_category="owner",
        active_context=None,
        is_active=True,
        binding_metadata=metadata,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _timeline(session, stamp: datetime):
    rows = []
    for days_ago in (2, 1, 0):
        row = TimelineEventRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            canonical_event_id=None,
            actor_id=ACTOR,
            relationship_id=None,
            resource_id=None,
            event_type="LOCATION_ARRIVAL",
            event_ref={"pattern_key": "gym-arrival"},
            occurred_at=stamp - timedelta(days=days_ago),
            visibility="PRIVATE",
            provenance="android-location",
            metadata_json={},
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _setup_runtime(session, stamp: datetime):
    binding = _owner(session)
    _timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    session.flush()
    return binding


def test_runtime_cycle_persists_pattern_recommendation_and_delivery(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    binding = _setup_runtime(session, stamp)

    result = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )

    assert result.owners_scanned == 1
    assert result.hypotheses_detected == 1
    assert result.hypotheses_persisted == 1
    assert result.recommendations_built == 1
    assert result.recommendations_persisted == 1
    assert result.recommendations_enqueued == 1
    assert result.recommendations_blocked_channel == 0

    pattern = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.pattern.temporal_recurrence",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    )
    interaction = (
        session.get(InteractionRow, outbox.interaction_id)
        if outbox is not None
        else None
    )

    assert pattern is not None
    assert pattern.object_json["hypothesis_status"] == "HYPOTHESIS"
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "PROPOSED"
    assert recommendation.object_json["execution_requested"] is False
    assert recommendation.object_json["grants_authority"] is False

    assert outbox is not None
    assert outbox.status == "PENDING"
    assert outbox.destination == "local_transport"
    assert outbox.execution_intent_id is None
    assert outbox.payload["external_actor_id"] == binding.external_actor_id
    assert outbox.payload["recommendation_id"] == (
        recommendation.context["recommendation_id"]
    )
    assert "Sugestão:" in outbox.payload["text"]
    assert "Não vou criar nada sem uma confirmação explícita." in (
        outbox.payload["text"]
    )
    assert interaction is not None
    assert interaction.state == "COMPLETED"
    assert interaction.event_type == "PERSONAL_CONTEXT_RECOMMENDATION"


def test_runtime_cycle_is_idempotent_across_worker_ticks(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _setup_runtime(session, stamp)

    first = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    second = run_personal_context_runtime_cycle(
        session,
        now=stamp + timedelta(minutes=1),
        delivery_enabled=True,
    )

    assert first.recommendations_enqueued == 1
    assert second.hypotheses_persisted == 0
    assert second.recommendations_persisted == 0
    assert second.recommendations_enqueued == 0
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate
            == "context.pattern.temporal_recurrence"
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate
            == "context.recommendation.proactive"
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(InteractionRow)
        .where(
            InteractionRow.event_type
            == "PERSONAL_CONTEXT_RECOMMENDATION"
        )
    ) == 1


def test_ambiguous_owner_channel_persists_but_does_not_enqueue(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _owner(session, external="owner-a@c.us", primary=False)
    _owner(session, external="owner-b@c.us", primary=False)
    _timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)

    result = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )

    assert result.recommendations_persisted == 1
    assert result.recommendations_enqueued == 0
    assert result.recommendations_blocked_channel == 1
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    ) == 0


def test_primary_owner_channel_wins_when_multiple_bindings_exist(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    preferred = _owner(
        session,
        external="preferred-owner@c.us",
        primary=True,
    )
    _owner(
        session,
        external="secondary-owner@c.us",
        primary=False,
    )
    _timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)

    result = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    )

    assert result.recommendations_enqueued == 1
    assert outbox is not None
    assert outbox.payload["external_actor_id"] == (
        preferred.external_actor_id
    )


def test_runtime_cycle_never_accepts_or_executes_recommendation(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _setup_runtime(session, stamp)

    run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )

    assert session.scalar(
        select(func.count()).select_from(ReminderRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(AgentExecutionIntentRow)
    ) == 0


class _TransportRecorder:
    def __init__(self):
        self.calls = []

    def dispatch_outbox(self, row):
        self.calls.append(row.id)
        return type(
            "Result",
            (),
            {
                "status": "sent",
                "response": {"message_reference": "msg-v1g"},
            },
        )()


def test_existing_outbox_dispatch_delivers_recommendation(session, monkeypatch):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _setup_runtime(session, stamp)
    run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    )
    recorder = _TransportRecorder()
    monkeypatch.setattr(
        services,
        "local_transport_outbound",
        recorder,
    )

    assert services.process_outbox(session, "v1g-worker") == 1
    session.refresh(outbox)

    assert outbox.status == "DONE"
    assert outbox.completed_at is not None
    assert recorder.calls == [outbox.id]
    delivered = session.scalar(
        select(AuditEventRow).where(
            AuditEventRow.event_type
            == "personal_context.recommendation_delivered"
        )
    )
    assert delivered is not None
    assert delivered.payload["recommendation_id"] == (
        outbox.payload["recommendation_id"]
    )


def test_worker_runtime_schedule_respects_feature_flag_and_interval(
    session,
    monkeypatch,
):
    calls = []

    def fake_cycle(*args, **kwargs):
        calls.append(kwargs)
        return PersonalContextRuntimeCycleResult(
            owners_scanned=1,
            recommendations_enqueued=1,
        )

    monkeypatch.setattr(
        worker,
        "run_personal_context_runtime_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_enabled",
        False,
    )

    result, last = worker.process_personal_context_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is None
    assert last is None
    assert calls == []

    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_interval_seconds",
        300,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_recommendation_delivery_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_runtime_owner_limit",
        25,
    )

    result, last = worker.process_personal_context_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is not None
    assert last == 100.0
    assert calls[-1]["delivery_enabled"] is True
    assert calls[-1]["owner_limit"] == 25

    result, last2 = worker.process_personal_context_runtime_if_due(
        session,
        now_monotonic=200.0,
        last_run_monotonic=last,
    )
    assert result is None
    assert last2 == 100.0
    assert len(calls) == 1

    result, last3 = worker.process_personal_context_runtime_if_due(
        session,
        now_monotonic=401.0,
        last_run_monotonic=last,
    )
    assert result is not None
    assert last3 == 401.0
    assert len(calls) == 2
