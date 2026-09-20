from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_RECOMMENDATION_ACTION,
    process_personal_context_recommendations,
)
from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-v1g"


def _install(session, stamp: datetime, *, delivery_binding: bool = True) -> None:
    if session.get(TenantRow, DEFAULT_TENANT_ID) is None:
        now = now_utc()
        session.add(
            TenantRow(
                id=DEFAULT_TENANT_ID,
                slug=DEFAULT_TENANT_ID,
                name="default",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()

    session.add(
        MemoryActorRow(
            id=new_id(),
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            metadata_json={},
            created_at=now_utc(),
            updated_at=now_utc(),
        )
    )
    if delivery_binding:
        upsert_actor_binding(
            session,
            "wwebjs",
            "owner-v1g-external",
            ACTOR,
            "owner",
            metadata={"owner": True},
            tenant_id=DEFAULT_TENANT_ID,
        )

    for index in range(3):
        session.add(
            TimelineEventRow(
                id=new_id(),
                tenant_id=DEFAULT_TENANT_ID,
                canonical_event_id=None,
                actor_id=ACTOR,
                relationship_id=None,
                resource_id=None,
                event_type="LOCATION_ARRIVAL",
                event_ref={"pattern_key": "gym-arrival"},
                occurred_at=stamp - timedelta(days=2 - index),
                visibility="PRIVATE",
                provenance="android-location",
                metadata_json={},
            )
        )
    sync_platform_registry(session)
    session.flush()


def _execution_side_effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(ExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_runtime_pump_detects_persists_and_enqueues_one_proposal(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install(session, stamp)

    before = _execution_side_effect_counts(session)
    count = process_personal_context_recommendations(
        session,
        now=stamp,
    )
    after = _execution_side_effect_counts(session)

    assert count == 1
    pattern = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.pattern.temporal_recurrence",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_ACTION
        )
    )

    assert pattern is not None
    assert pattern.object_json["hypothesis_status"] == "HYPOTHESIS"
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "PROPOSED"
    assert recommendation.object_json["execution_requested"] is False
    assert recommendation.object_json["grants_authority"] is False
    assert outbox is not None
    assert outbox.destination == "local_transport"
    assert outbox.status == "PENDING"
    assert outbox.execution_intent_id is None
    assert outbox.payload["requires_user_confirmation"] is True
    assert outbox.payload["recommendation_id"] == (
        recommendation.context["recommendation_id"]
    )
    assert "Quer que eu prepare um lembrete" in outbox.payload["text"]
    assert after == before


def test_runtime_pump_replay_is_idempotent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install(session, stamp)

    first = process_personal_context_recommendations(session, now=stamp)
    second = process_personal_context_recommendations(session, now=stamp)

    assert first == 1
    assert second == 0
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_ACTION
        )
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    ) == 1


def test_missing_owner_delivery_binding_keeps_proposal_without_outbox(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install(session, stamp, delivery_binding=False)

    count = process_personal_context_recommendations(session, now=stamp)

    assert count == 0
    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert recommendation is not None
    assert recommendation.object_json["lifecycle_state"] == "PROPOSED"
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_ACTION
        )
    ) == 0


def test_runtime_pump_never_advances_proposal_into_execution(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install(session, stamp)

    process_personal_context_recommendations(session, now=stamp)
    process_personal_context_recommendations(
        session,
        now=stamp + timedelta(minutes=5),
    )

    assert _execution_side_effect_counts(session) == (0, 0, 0, 0)
