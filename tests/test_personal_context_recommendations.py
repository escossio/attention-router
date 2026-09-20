from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application.personal_context_recommendations import (
    REMINDER_CAPABILITY,
    build_context_recommendations,
)
from attention_router.application.platform.registry import sync_capability_definitions
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
    TenantRow,
)


ACTOR = "owner-recommendation"


def _ensure_tenant(session, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug=tenant_id,
            name=tenant_id,
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _claim(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    actor_key: str = ACTOR,
    now: datetime,
    event_type: str = "LOCATION_ARRIVAL",
    confidence: float = 0.85,
    support_ratio: float = 1.0,
    valid_until=None,
) -> MemoryClaimRow:
    _ensure_tenant(session, tenant_id)
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=tenant_id,
        actor_key=actor_key,
        metadata_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(actor)
    session.flush()
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate="context.pattern.temporal_recurrence",
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": "TEMPORAL_RECURRENCE",
            "event_type": event_type,
            "signature_kind": "PATTERN_KEY",
            "signature_value": "gym-arrival",
            "cadence_seconds": 24 * 60 * 60,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": "pattern-gym",
            "snapshot_fingerprint": "sha256:test",
            "evidence_timeline_event_ids": ["evt-1", "evt-2", "evt-3"],
            "source_provenance": ["android-location", "calendar"],
            "occurrence_count": 3,
            "anomaly_count": 0,
            "support_ratio": support_ratio,
        },
        confidence=confidence,
        sensitivity_class="PRIVATE",
        source_quality="DERIVED_PATTERN",
        valid_from=now - timedelta(days=3),
        valid_until=valid_until or now + timedelta(days=3),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=now - timedelta(days=2),
        last_observed_at=now,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_active_recurrence_yields_explainable_proposal_without_execution(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _ensure_tenant(session)
    sync_capability_definitions(session)
    claim = _claim(session, now=stamp)

    before = _counts(session)
    items = build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    )
    after = _counts(session)

    assert len(items) == 1
    item = items[0]
    assert item.source_claim_id == claim.id
    assert item.recommendation_type == "REMINDER_FOR_RECURRENT_ARRIVAL"
    assert item.status == "PROPOSED"
    assert item.capability_name == REMINDER_CAPABILITY
    assert item.capability_availability == "OPERATIONAL"
    assert item.requires_user_confirmation is True
    assert item.execution_requested is False
    assert item.grants_authority is False
    assert item.confidence == 0.85
    assert item.support_ratio == 1.0
    assert item.occurrence_count == 3
    assert item.source_provenance == ("android-location", "calendar")
    assert "3 ocorrências" in item.explanation
    assert "24 horas" in item.explanation
    assert item.suggested_parameters["summary"] == "Recorrência observada pela Andy"
    assert item.suggested_parameters["trigger_at"] == (
        stamp + timedelta(days=1)
    ).isoformat()
    assert item.valid_until == stamp + timedelta(days=1)
    assert after == before


def test_recommendation_id_is_stable_for_same_claim_and_next_occurrence(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _ensure_tenant(session)
    sync_capability_definitions(session)
    _claim(session, now=stamp)

    first = build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    )
    second = build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    )

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].recommendation_id == second[0].recommendation_id


def test_unregistered_capability_blocks_recommendation(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _claim(session, now=stamp)

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    ) == ()


def test_low_confidence_or_low_support_hypothesis_is_not_recommended(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _ensure_tenant(session)
    sync_capability_definitions(session)
    _claim(
        session,
        actor_key="low-confidence",
        now=stamp,
        confidence=0.79,
    )
    _claim(
        session,
        actor_key="low-support",
        now=stamp,
        support_ratio=0.74,
    )

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key="low-confidence",
        now=stamp,
    ) == ()
    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key="low-support",
        now=stamp,
    ) == ()


def test_expired_hypothesis_is_not_recommended(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _ensure_tenant(session)
    sync_capability_definitions(session)
    _claim(
        session,
        now=stamp,
        valid_until=stamp - timedelta(seconds=1),
    )

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    ) == ()


def test_non_location_recurrence_stays_hypothesis_only(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _ensure_tenant(session)
    sync_capability_definitions(session)
    _claim(
        session,
        now=stamp,
        event_type="MESSAGE_RECEIVED",
    )

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    ) == ()


def test_actor_and_tenant_scope_are_fail_closed(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    tenant_b = "00000000-0000-4000-8000-0000000000d2"
    _ensure_tenant(session)
    _ensure_tenant(session, tenant_b)
    sync_capability_definitions(session)
    sync_capability_definitions(session, tenant_id=tenant_b)
    _claim(
        session,
        tenant_id=tenant_b,
        actor_key=ACTOR,
        now=stamp,
    )

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    ) == ()
    assert len(
        build_context_recommendations(
            session,
            tenant_id=tenant_b,
            actor_key=ACTOR,
            now=stamp,
        )
    ) == 1
