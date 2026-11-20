from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context import build_personal_context
from attention_router.application.personal_context_corrections import (
    PatternCorrectionError,
    correct_context_pattern_hypothesis,
)
from attention_router.application.personal_context_hypotheses import (
    ContextHypothesisPersistenceError,
    PATTERN_CORRECTION_PREDICATE,
    PATTERN_CORRECTION_SOURCE_QUALITY,
    persist_context_pattern_hypothesis,
)
from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.application.personal_context_runtime import (
    PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
    run_personal_context_runtime_cycle,
)
from attention_router.application.platform.capability_pack import (
    provision_internal_providers,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryClaimRow,
    OutboxMessageRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-v1h"
EXTERNAL = "owner-v1h@c.us"


def _ensure_tenant(session) -> None:
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


def _install_owner(session) -> None:
    _ensure_tenant(session)
    upsert_actor_binding(
        session,
        "wwebjs",
        EXTERNAL,
        ACTOR,
        "owner",
        metadata={
            "owner": True,
            "owner_channel_role": "PRIMARY_OWNER_WHATSAPP",
        },
        tenant_id=DEFAULT_TENANT_ID,
    )


def _timeline_event(session, occurred_at: datetime) -> TimelineEventRow:
    row = TimelineEventRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        canonical_event_id=None,
        actor_id=ACTOR,
        relationship_id=None,
        resource_id=None,
        event_type="LOCATION_ARRIVAL",
        event_ref={"pattern_key": "gym-arrival"},
        occurred_at=occurred_at,
        visibility="PRIVATE",
        provenance="android-location",
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def _initial_timeline(session, stamp: datetime) -> None:
    for days_ago in (2, 1, 0):
        _timeline_event(session, stamp - timedelta(days=days_ago))


def _owner_event(
    session,
    *,
    stamp: datetime,
    external_event_id: str,
    authenticated: bool = True,
) -> InboundEventRow:
    payload = {
        "actor_id": EXTERNAL,
        "content": "isso não é uma rotina",
        "event_origin": "OWNER_COMMAND",
        "owner_authenticated": authenticated,
        "metadata": {
            "from_me": True,
            "owner_self_chat": True,
            "from_me_classification": "OWNER_COMMAND",
            "final_from_me_classification": "OWNER_COMMAND",
        },
    }
    row = InboundEventRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        source="wwebjs",
        external_event_id=external_event_id,
        event_type="message",
        payload=payload,
        payload_hash=stable_hash(payload),
        received_at=stamp,
        processed_at=None,
        interaction_id=None,
        status="RECEIVED",
        error=None,
        correlation_id=new_id(),
        lineage_classification="ORGANIC",
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    session.add(row)
    session.flush()
    return row


def _active_pattern(session) -> MemoryClaimRow:
    row = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate
            == "context.pattern.temporal_recurrence",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert row is not None
    return row


def test_owner_correction_suppresses_pattern_and_invalidates_delivery(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install_owner(session)
    _initial_timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)

    first = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    assert first.hypotheses_persisted == 1
    assert first.recommendations_enqueued == 1

    pattern = _active_pattern(session)
    hypothesis_id = pattern.context["hypothesis_id"]
    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    outbox = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION
        )
    )
    assert recommendation is not None
    assert outbox is not None
    assert outbox.status == "PENDING"

    correction_event = _owner_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        external_event_id="v1h-correction-1",
    )
    outcome = correct_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis_id,
        correction_event=correction_event,
    )

    session.refresh(pattern)
    session.refresh(recommendation)
    session.refresh(outbox)
    correction = session.get(MemoryClaimRow, outcome.correction_claim_id)

    assert outcome.changed is True
    assert pattern.status == "SUPERSEDED"
    assert recommendation.status == "SUPERSEDED"
    assert outbox.status == "CANCELED"
    assert correction is not None
    assert correction.status == "ACTIVE"
    assert correction.predicate == PATTERN_CORRECTION_PREDICATE
    assert correction.source_quality == PATTERN_CORRECTION_SOURCE_QUALITY
    assert correction.object_json["evidence_class"] == "EXPLICITLY_CONFIRMED"
    assert correction.object_json["grants_authority"] is False
    assert correction.supersedes_claim_id == pattern.id
    assert correction.context["correction_inbound_event_id"] == (
        correction_event.id
    )
    assert session.scalar(select(func.count()).select_from(FactRow)) == 0

    second = run_personal_context_runtime_cycle(
        session,
        now=stamp + timedelta(minutes=2),
        delivery_enabled=True,
    )
    assert second.hypotheses_detected == 1
    assert second.hypotheses_persisted == 0
    assert second.recommendations_built == 0
    assert second.recommendations_enqueued == 0

    snapshot = build_personal_context(
        session,
        DEFAULT_TENANT_ID,
        now=stamp + timedelta(minutes=2),
    )
    active_ids = {item.claim_id for item in snapshot.claims}
    assert pattern.id not in active_ids
    assert correction.id in active_ids

    replay = correct_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis_id,
        correction_event=correction_event,
    )
    assert replay.changed is False
    assert replay.correction_claim_id == correction.id


def test_unauthenticated_owner_correction_fails_closed(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install_owner(session)
    _initial_timeline(session, stamp)
    hypothesis = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    pattern, _ = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    event = _owner_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        external_event_id="v1h-correction-unauth",
        authenticated=False,
    )

    with pytest.raises(
        PatternCorrectionError,
        match="PATTERN_CORRECTION_OWNER_AUTHORITY_UNAVAILABLE",
    ):
        correct_context_pattern_hypothesis(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            hypothesis_id=hypothesis.hypothesis_id,
            correction_event=event,
        )

    session.refresh(pattern)
    assert pattern.status == "ACTIVE"
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(MemoryClaimRow.predicate == PATTERN_CORRECTION_PREDICATE)
    ) == 0


def test_three_post_correction_observations_allow_relearning(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install_owner(session)
    _initial_timeline(session, stamp)
    hypothesis = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    original, _ = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    correction_event = _owner_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        external_event_id="v1h-correction-relearn",
    )
    outcome = correct_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis.hypothesis_id,
        correction_event=correction_event,
    )
    correction = session.get(MemoryClaimRow, outcome.correction_claim_id)
    assert correction is not None

    with pytest.raises(
        ContextHypothesisPersistenceError,
        match="PATTERN_HYPOTHESIS_OWNER_CORRECTED",
    ):
        persist_context_pattern_hypothesis(
            session,
            hypothesis=hypothesis,
            now=stamp + timedelta(minutes=2),
        )

    for days_after in (1, 2, 3):
        _timeline_event(session, stamp + timedelta(days=days_after))

    relearned_hypothesis = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp + timedelta(days=3),
    )[0]
    relearned, changed = persist_context_pattern_hypothesis(
        session,
        hypothesis=relearned_hypothesis,
        now=stamp + timedelta(days=3),
    )

    session.refresh(correction)
    assert changed is True
    assert relearned.id != original.id
    assert relearned.status == "ACTIVE"
    assert relearned.supersedes_claim_id == correction.id
    assert correction.status == "SUPERSEDED"
    assert relearned.context["occurrence_count"] >= 6


def test_correction_retires_only_same_tenant_execution_intent(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install_owner(session)
    _initial_timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    result = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=False,
    )
    assert result.recommendations_persisted == 1

    pattern = _active_pattern(session)
    recommendation = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert recommendation is not None
    recommendation_id = recommendation.context["recommendation_id"]

    same_scope = {
        "tenant_id": DEFAULT_TENANT_ID,
        "recommendation_id": recommendation_id,
        "recommendation_claim_id": recommendation.id,
    }
    same_tenant_intent = ExecutionIntentRow(
        id=new_id(),
        idempotency_key="v1h-same-tenant-intent",
        scope=same_scope,
        scope_fingerprint=stable_hash(same_scope),
        provenance={"origin": "test"},
        state="PREPARED",
        created_at=stamp,
        frozen_at=None,
        retired_at=None,
        authority_profile_id=None,
        expires_at=stamp + timedelta(days=1),
    )
    other_scope = {
        "tenant_id": "tenant-other",
        "recommendation_id": recommendation_id,
        "recommendation_claim_id": recommendation.id,
    }
    other_tenant_intent = ExecutionIntentRow(
        id=new_id(),
        idempotency_key="v1h-other-tenant-intent",
        scope=other_scope,
        scope_fingerprint=stable_hash(other_scope),
        provenance={"origin": "test"},
        state="PREPARED",
        created_at=stamp,
        frozen_at=None,
        retired_at=None,
        authority_profile_id=None,
        expires_at=stamp + timedelta(days=1),
    )
    session.add_all([same_tenant_intent, other_tenant_intent])
    session.flush()

    event = _owner_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        external_event_id="v1h-correction-intent-isolation",
    )
    outcome = correct_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=pattern.context["hypothesis_id"],
        correction_event=event,
    )

    session.refresh(same_tenant_intent)
    session.refresh(other_tenant_intent)
    assert same_tenant_intent.state == "RETIRED"
    assert same_tenant_intent.id in outcome.retired_execution_intent_ids
    assert other_tenant_intent.state == "PREPARED"
    assert other_tenant_intent.id not in outcome.retired_execution_intent_ids


def test_correction_invalidates_recommendations_from_entire_hypothesis_lineage(
    session,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _install_owner(session)
    _initial_timeline(session, stamp)
    provision_internal_providers(session, DEFAULT_TENANT_ID)

    first = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )
    assert first.hypotheses_persisted == 1
    assert first.recommendations_enqueued == 1

    _timeline_event(session, stamp + timedelta(hours=23))
    second = run_personal_context_runtime_cycle(
        session,
        now=stamp + timedelta(hours=23),
        delivery_enabled=True,
    )
    assert second.hypotheses_persisted == 1
    assert second.recommendations_enqueued == 1

    active_patterns = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.pattern.temporal_recurrence",
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    assert len(active_patterns) == 1
    hypothesis_id = active_patterns[0].context["hypothesis_id"]

    active_recommendations = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    assert len(active_recommendations) == 2
    assert len({row.context["source_claim_id"] for row in active_recommendations}) == 2

    pending_outbox = session.scalars(
        select(OutboxMessageRow).where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
            OutboxMessageRow.status == "PENDING",
        )
    ).all()
    assert len(pending_outbox) == 2

    correction_event = _owner_event(
        session,
        stamp=stamp + timedelta(hours=23, minutes=1),
        external_event_id="v1h-correction-lineage",
    )
    outcome = correct_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis_id,
        correction_event=correction_event,
    )

    assert len(outcome.invalidated_recommendation_ids) == 2
    assert len(outcome.canceled_outbox_ids) == 2
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == "context.recommendation.proactive",
            MemoryClaimRow.status == "ACTIVE",
        )
    ) == 0
    assert session.scalar(
        select(func.count())
        .select_from(OutboxMessageRow)
        .where(
            OutboxMessageRow.action_type
            == PERSONAL_CONTEXT_RECOMMENDATION_OUTBOX_ACTION,
            OutboxMessageRow.status == "PENDING",
        )
    ) == 0
