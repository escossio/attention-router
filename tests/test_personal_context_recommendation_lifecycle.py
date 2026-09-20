from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RecommendationDecision,
    RecommendationLifecycleError,
    expire_due_context_recommendations,
    persist_context_recommendation,
    resolve_context_recommendation,
)
from attention_router.application.personal_context_recommendations import (
    ContextRecommendation,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentExecutionIntentRow,
    FactRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
    ReminderRow,
    TenantRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-recommendation-lifecycle"
EXTERNAL_ACTOR = "owner-recommendation-external"


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


def _install_owner(session) -> MemoryActorRow:
    _ensure_tenant(session)
    upsert_actor_binding(
        session,
        "wwebjs",
        EXTERNAL_ACTOR,
        ACTOR,
        "owner",
        metadata={"owner": True},
        tenant_id=DEFAULT_TENANT_ID,
    )
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        metadata_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(actor)
    session.flush()
    return actor


def _source_claim(
    session,
    *,
    actor: MemoryActorRow,
    stamp: datetime,
    suffix: str = "a",
) -> MemoryClaimRow:
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
            "event_type": "LOCATION_ARRIVAL",
            "signature_kind": "PATTERN_KEY",
            "signature_value": f"gym-arrival-{suffix}",
            "cadence_seconds": 86400,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": f"pattern-{suffix}",
            "source_provenance": ["android-location"],
            "occurrence_count": 3,
            "anomaly_count": 0,
            "support_ratio": 1.0,
        },
        confidence=0.85,
        sensitivity_class="PRIVATE",
        source_quality="DERIVED_PATTERN",
        valid_from=stamp - timedelta(days=3),
        valid_until=stamp + timedelta(days=3),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp - timedelta(days=2),
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _recommendation(
    *,
    source_claim: MemoryClaimRow,
    stamp: datetime,
    suffix: str = "a",
    valid_until=None,
) -> ContextRecommendation:
    return ContextRecommendation(
        recommendation_id=f"recommendation-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        source_claim_id=source_claim.id,
        recommendation_type="REMINDER_FOR_RECURRENT_ARRIVAL",
        status="PROPOSED",
        explanation="Quer que eu prepare um lembrete para a próxima ocorrência prevista?",
        capability_name="reminder.create",
        capability_availability="OPERATIONAL",
        suggested_parameters={
            "summary": "Recorrência observada pela Andy",
            "trigger_at": (stamp + timedelta(days=1)).isoformat(),
        },
        confidence=0.85,
        support_ratio=1.0,
        occurrence_count=3,
        anomaly_count=0,
        source_provenance=("android-location",),
        generated_at=stamp,
        valid_until=valid_until or stamp + timedelta(days=1),
        requires_user_confirmation=True,
        execution_requested=False,
        grants_authority=False,
    )


def _decision_event(
    session,
    *,
    stamp: datetime,
    event_id: str,
    authenticated: bool = True,
) -> InboundEventRow:
    payload = {
        "actor_id": EXTERNAL_ACTOR,
        "content": "sim",
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
        external_event_id=event_id,
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


def _side_effect_counts(session) -> tuple[int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(ReminderRow)),
        session.scalar(select(func.count()).select_from(OutboxMessageRow)),
        session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)),
        session.scalar(select(func.count()).select_from(FactRow)),
    )


def test_proposed_recommendation_persists_without_execution(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)

    before = _side_effect_counts(session)
    row, changed = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    after = _side_effect_counts(session)

    assert changed is True
    assert row.predicate == RECOMMENDATION_CLAIM_PREDICATE
    assert row.status == "ACTIVE"
    assert row.object_json["lifecycle_state"] == "PROPOSED"
    assert row.object_json["requires_user_confirmation"] is True
    assert row.object_json["execution_requested"] is False
    assert row.object_json["grants_authority"] is False
    assert row.context["recommendation_id"] == recommendation.recommendation_id
    assert after == before


def test_exact_proposal_replay_is_idempotent(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)

    first, first_changed = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    second, second_changed = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )

    assert first_changed is True
    assert second_changed is False
    assert first.id == second.id


@pytest.mark.parametrize(
    ("decision", "expected_state"),
    [
        (RecommendationDecision.ACCEPT, "ACCEPTED"),
        (RecommendationDecision.DISMISS, "DISMISSED"),
    ],
)
def test_explicit_owner_decision_transitions_without_execution(
    session,
    decision,
    expected_state,
):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)
    proposed, _ = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        event_id=f"decision-{expected_state.lower()}",
    )

    before = _side_effect_counts(session)
    terminal, changed = resolve_context_recommendation(
        session,
        recommendation_id=recommendation.recommendation_id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision=decision,
        resolution_event=event,
    )
    after = _side_effect_counts(session)
    session.refresh(proposed)

    assert changed is True
    assert proposed.status == "SUPERSEDED"
    assert terminal.status == "ACTIVE"
    assert terminal.supersedes_claim_id == proposed.id
    assert terminal.object_json["lifecycle_state"] == expected_state
    assert terminal.object_json["execution_requested"] is False
    assert terminal.object_json["grants_authority"] is False
    assert terminal.context["resolution_inbound_event_id"] == event.id
    assert terminal.context["resolution_kind"] == decision.value
    assert after == before


def test_same_resolution_event_replay_is_idempotent(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)
    persist_context_recommendation(session, recommendation=recommendation)
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        event_id="decision-replay",
    )

    first, first_changed = resolve_context_recommendation(
        session,
        recommendation_id=recommendation.recommendation_id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision="ACCEPT",
        resolution_event=event,
    )
    second, second_changed = resolve_context_recommendation(
        session,
        recommendation_id=recommendation.recommendation_id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision="ACCEPT",
        resolution_event=event,
    )

    assert first_changed is True
    assert second_changed is False
    assert first.id == second.id


def test_resolution_event_cannot_resolve_two_recommendations(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source_a = _source_claim(session, actor=actor, stamp=stamp, suffix="a")
    source_b = _source_claim(session, actor=actor, stamp=stamp, suffix="b")
    rec_a = _recommendation(source_claim=source_a, stamp=stamp, suffix="a")
    rec_b = _recommendation(source_claim=source_b, stamp=stamp, suffix="b")
    persist_context_recommendation(session, recommendation=rec_a)
    persist_context_recommendation(session, recommendation=rec_b)
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        event_id="decision-shared",
    )

    resolve_context_recommendation(
        session,
        recommendation_id=rec_a.recommendation_id,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        decision="ACCEPT",
        resolution_event=event,
    )

    with pytest.raises(
        RecommendationLifecycleError,
        match="RECOMMENDATION_DECISION_EVENT_REUSED",
    ):
        resolve_context_recommendation(
            session,
            recommendation_id=rec_b.recommendation_id,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            decision="ACCEPT",
            resolution_event=event,
        )


def test_unauthenticated_decision_is_rejected_and_proposal_remains(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)
    proposed, _ = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=1),
        event_id="decision-unauthenticated",
        authenticated=False,
    )

    with pytest.raises(
        RecommendationLifecycleError,
        match="RECOMMENDATION_DECISION_OWNER_AUTHORITY_UNAVAILABLE",
    ):
        resolve_context_recommendation(
            session,
            recommendation_id=recommendation.recommendation_id,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            decision="ACCEPT",
            resolution_event=event,
        )

    session.refresh(proposed)
    assert proposed.status == "ACTIVE"
    assert proposed.object_json["lifecycle_state"] == "PROPOSED"


def test_due_proposal_expires_without_execution(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(
        source_claim=source,
        stamp=stamp,
        valid_until=stamp + timedelta(hours=1),
    )
    proposed, _ = persist_context_recommendation(
        session,
        recommendation=recommendation,
    )

    before = _side_effect_counts(session)
    expired_count = expire_due_context_recommendations(
        session,
        now=stamp + timedelta(hours=2),
    )
    after = _side_effect_counts(session)
    session.refresh(proposed)

    current = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.supersedes_claim_id == proposed.id
        )
    )
    assert expired_count == 1
    assert proposed.status == "SUPERSEDED"
    assert current is not None
    assert current.object_json["lifecycle_state"] == "EXPIRED"
    assert current.object_json["execution_requested"] is False
    assert current.object_json["grants_authority"] is False
    assert after == before


def test_accept_after_expiry_is_rejected_without_execution(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(
        source_claim=source,
        stamp=stamp,
        valid_until=stamp + timedelta(minutes=5),
    )
    persist_context_recommendation(session, recommendation=recommendation)
    event = _decision_event(
        session,
        stamp=stamp + timedelta(minutes=10),
        event_id="decision-too-late",
    )

    before = _side_effect_counts(session)
    with pytest.raises(
        RecommendationLifecycleError,
        match="RECOMMENDATION_EXPIRED",
    ):
        resolve_context_recommendation(
            session,
            recommendation_id=recommendation.recommendation_id,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            decision="ACCEPT",
            resolution_event=event,
        )
    after = _side_effect_counts(session)

    assert after == before



def test_forged_capability_or_confidence_is_rejected(session):
    actor = _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    source = _source_claim(session, actor=actor, stamp=stamp)
    recommendation = _recommendation(source_claim=source, stamp=stamp)

    with pytest.raises(
        RecommendationLifecycleError,
        match="RECOMMENDATION_CAPABILITY_INVALID",
    ):
        persist_context_recommendation(
            session,
            recommendation=replace(
                recommendation,
                capability_name="presence.set",
            ),
        )

    with pytest.raises(
        RecommendationLifecycleError,
        match="RECOMMENDATION_SOURCE_CONFIDENCE_MISMATCH",
    ):
        persist_context_recommendation(
            session,
            recommendation=replace(
                recommendation,
                confidence=0.95,
            ),
        )

    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE
        )
    ) == 0
