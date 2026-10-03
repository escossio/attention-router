from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.attention import (
    AttentionError,
    acknowledge_attention_assessment,
    assess_missing_step_attention,
    assess_obligation_attention,
    build_owner_suggestion_candidate,
    suppress_attention_assessment,
)
from attention_router.application.obligations import (
    advance_due_obligation_states,
    reconcile_obligation_event,
)
from attention_router.application.personal_context_controls import (
    CONTEXT_CONTROL_PREDICATE,
    CONTEXT_CONTROL_SOURCE_QUALITY,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.attention_models import AttentionAssessmentRow
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    MemoryActorRow,
    MemoryClaimRow,
    OutboxMessageRow,
)
from tests.test_obligations import _instance, _payment


def _attention_anomaly(
    session,
    *,
    suffix: str,
    now: datetime,
    age: timedelta,
    confidence: float = 0.8,
    support_ratio: float = 1.0,
    occurrence_count: int = 5,
    signature_kind: str = "RELATIONSHIP",
):
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=f"actor-attention-{suffix}",
        metadata_json={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(actor)
    session.flush()

    hypothesis_id = f"attention-hypothesis-{suffix}"
    sequence = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate="context.pattern.event_sequence",
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": "EVENT_SEQUENCE",
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
        },
        context={
            "hypothesis_id": hypothesis_id,
            "support_ratio": support_ratio,
            "occurrence_count": occurrence_count,
        },
        confidence=0.9,
        sensitivity_class="PRIVATE",
        source_quality="DERIVED_PATTERN",
        valid_from=now - timedelta(days=30),
        valid_until=now + timedelta(days=30),
        status="ACTIVE",
        staleness_class="STABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=now - timedelta(days=30),
        last_observed_at=now - age,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(sequence)
    session.flush()

    anomaly = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate="context.pattern.sequence_anomaly",
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": "SEQUENCE_ANOMALY",
            "anomaly_type": "MISSING_EXPECTED_STEP",
            "expected_second_event_type": "EXPECTED_EVENT",
            "expected_second_signature_kind": signature_kind,
            "expected_second_signature_value": f"signature-{suffix}",
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "anomaly_id": f"anomaly-{suffix}",
            "source_sequence_claim_id": sequence.id,
            "source_hypothesis_id": hypothesis_id,
        },
        confidence=confidence,
        sensitivity_class="PRIVATE",
        source_quality="DERIVED_PATTERN",
        valid_from=now - age,
        valid_until=now + timedelta(days=1),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=now - age,
        last_observed_at=now - age,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(anomaly)
    session.flush()
    return actor, sequence, anomaly, hypothesis_id


def _set_non_actionable(
    session,
    *,
    actor: MemoryActorRow,
    hypothesis_id: str,
    now: datetime,
):
    control = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=CONTEXT_CONTROL_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "control_type": "OWNER_CONTEXT_POLICY",
            "privacy": "DEFAULT",
            "actionability": "NON_ACTIONABLE",
            "evidence_class": "USER_DECLARED",
            "grants_authority": False,
            "grants_disclosure_authority": False,
        },
        context={
            "target_kind": "PATTERN_HYPOTHESIS",
            "target_hypothesis_id": hypothesis_id,
        },
        confidence=1.0,
        sensitivity_class="PRIVATE",
        source_quality=CONTEXT_CONTROL_SOURCE_QUALITY,
        valid_from=now,
        valid_until=None,
        status="ACTIVE",
        staleness_class="STABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=now,
        last_observed_at=now,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(control)
    session.flush()
    return control


def _overdue_instance(session, *, suffix: str, now: datetime):
    owner, _angelo, _resource, _definition, instance = _instance(
        session,
        suffix=suffix,
    )
    changed = advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=now,
    )
    assert changed >= 1
    session.refresh(instance)
    assert instance.state == "UNCONFIRMED_AFTER_DUE"
    return owner, instance


def test_v2h_low_value_stale_anomaly_stays_silent(session):
    stamp = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    _actor, _sequence, anomaly, _hypothesis = _attention_anomaly(
        session,
        suffix="stale",
        now=stamp,
        age=timedelta(days=6),
        confidence=0.8,
        support_ratio=1.0,
        occurrence_count=3,
        signature_kind="PATTERN_KEY",
    )

    assessment, created = assess_missing_step_attention(
        session,
        anomaly_claim_id=anomaly.id,
        now=stamp,
    )

    assert created is True
    assert assessment.score < 0.65
    assert assessment.score_class == "IGNORE"
    assert assessment.effective_class == "IGNORE"
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp,
    ) is None
    assert assessment.provenance["grants_authority"] is False
    assert assessment.provenance["creates_outbound"] is False


def test_v2h_unconfirmed_obligation_reaches_bounded_suggestion_boundary(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _owner, instance = _overdue_instance(
        session,
        suffix="meaningful",
        now=stamp,
    )
    before = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    assessment, created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )
    candidate = build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp,
    )

    assert created is True
    assert assessment.score >= 0.82
    assert assessment.score_class == "OWNER_SUGGESTION_CANDIDATE"
    assert assessment.effective_class == "OWNER_SUGGESTION_CANDIDATE"
    assert "EXPECTATION_VIOLATION" in assessment.reason_codes
    assert "OWNER_RELEVANT" in assessment.reason_codes
    assert candidate is not None
    assert candidate.requires_user_confirmation is True
    assert candidate.execution_requested is False
    assert candidate.grants_authority is False
    assert before == {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }


def test_v2h_suggestion_boundary_revalidates_obligation_state(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _owner, angelo, resource, _definition, instance = _instance(
        session,
        suffix="revalidate",
    )
    advance_due_obligation_states(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        now=stamp,
    )
    session.refresh(instance)
    assessment, _ = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp,
    ) is not None

    payment = _payment(
        session,
        actor_key=angelo,
        resource_id=resource.id,
        suffix="revalidate",
        occurred_at=datetime(2026, 10, 10, 15, 0, tzinfo=UTC),
    )
    reconcile_obligation_event(
        session,
        instance_id=instance.id,
        timeline_event_id=payment.id,
        now=stamp + timedelta(minutes=1),
    )
    session.refresh(instance)
    assert instance.state == "SATISFIED"

    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp + timedelta(minutes=2),
    ) is None


def test_v2h_owner_non_actionable_caps_high_anomaly_to_reasoning(session):
    stamp = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    actor, _sequence, anomaly, hypothesis_id = _attention_anomaly(
        session,
        suffix="non-actionable",
        now=stamp,
        age=timedelta(hours=1),
        confidence=1.0,
        support_ratio=1.0,
        occurrence_count=5,
        signature_kind="RELATIONSHIP",
    )
    _set_non_actionable(
        session,
        actor=actor,
        hypothesis_id=hypothesis_id,
        now=stamp,
    )

    assessment, _ = assess_missing_step_attention(
        session,
        anomaly_claim_id=anomaly.id,
        now=stamp,
    )

    assert assessment.score_class == "OWNER_SUGGESTION_CANDIDATE"
    assert assessment.effective_class == "REASONING_QUEUE"
    assert "OWNER_NON_ACTIONABLE_CAP" in assessment.reason_codes
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp,
    ) is None


def test_v2h_cooldown_deduplicates_repeated_high_salience_snapshot(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _owner, instance = _overdue_instance(
        session,
        suffix="dedup",
        now=stamp,
    )
    first, first_created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )
    assert first_created is True
    assert first.effective_class == "OWNER_SUGGESTION_CANDIDATE"
    assert first.cooldown_until is not None

    instance.updated_at = stamp + timedelta(minutes=10)
    session.flush()

    second, second_created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp + timedelta(minutes=10),
    )
    session.refresh(first)

    assert second_created is True
    assert second.id != first.id
    assert first.status == "SUPERSEDED"
    assert second.score_class == "OWNER_SUGGESTION_CANDIDATE"
    assert second.effective_class == "REASONING_QUEUE"
    assert "DEDUP_SUPPRESSION_WINDOW" in second.reason_codes
    assert second.cooldown_until is not None
    assert first.cooldown_until is not None
    assert second.cooldown_until.replace(tzinfo=UTC) == (
        first.cooldown_until.replace(tzinfo=UTC)
    )


def test_v2h_owner_suppression_is_bounded_and_reversible(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    owner, instance = _overdue_instance(
        session,
        suffix="suppress",
        now=stamp,
    )
    assessment, _ = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )

    suppressed = suppress_attention_assessment(
        session,
        assessment_id=assessment.id,
        decision_actor_key=owner,
        decision_ref="owner-command:attention-suppress",
        until=stamp + timedelta(hours=2),
        now=stamp,
    )
    assert suppressed.status == "SUPPRESSED"
    assert suppressed.effective_class == "IGNORE"
    assert suppressed.owner_suppressed_by == owner
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp + timedelta(hours=1),
    ) is None

    replay, created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp + timedelta(hours=1),
    )
    assert created is False
    assert replay.id == assessment.id
    assert replay.status == "SUPPRESSED"

    resumed, created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp + timedelta(hours=3),
    )
    assert created is False
    assert resumed.status == "ACTIVE"
    assert resumed.effective_class == "OWNER_SUGGESTION_CANDIDATE"
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=resumed.id,
        now=stamp + timedelta(hours=3),
    ) is not None


def test_v2h_acknowledgement_stops_same_snapshot_from_reappearing(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    owner, instance = _overdue_instance(
        session,
        suffix="ack",
        now=stamp,
    )
    assessment, _ = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )
    acknowledged = acknowledge_attention_assessment(
        session,
        assessment_id=assessment.id,
        decision_actor_key=owner,
        decision_ref="owner-command:attention-ack",
        now=stamp + timedelta(minutes=1),
    )

    assert acknowledged.status == "ACKNOWLEDGED"
    assert acknowledged.acknowledged_by == owner
    assert "OWNER_ACKNOWLEDGED" in acknowledged.reason_codes
    assert build_owner_suggestion_candidate(
        session,
        assessment_id=assessment.id,
        now=stamp + timedelta(minutes=2),
    ) is None

    replay, created = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp + timedelta(minutes=2),
    )
    assert created is False
    assert replay.status == "ACKNOWLEDGED"


def test_v2h_secret_source_fails_closed_without_persisting_assessment(session):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _owner, instance = _overdue_instance(
        session,
        suffix="secret",
        now=stamp,
    )
    instance.sensitivity_class = "SECRET"
    session.flush()

    before = session.scalar(
        select(func.count()).select_from(AttentionAssessmentRow)
    )
    with pytest.raises(
        AttentionError,
        match="ATTENTION_SECRET_SOURCE_EXCLUDED",
    ):
        assess_obligation_attention(
            session,
            instance_id=instance.id,
            now=stamp,
        )
    assert session.scalar(
        select(func.count()).select_from(AttentionAssessmentRow)
    ) == before


def test_v2h_components_are_bounded_and_reason_codes_do_not_contain_source_data(
    session,
):
    stamp = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _owner, instance = _overdue_instance(
        session,
        suffix="bounded",
        now=stamp,
    )
    assessment, _ = assess_obligation_attention(
        session,
        instance_id=instance.id,
        now=stamp,
    )

    assert set(assessment.components) == {
        "impact",
        "urgency",
        "novelty",
        "confidence",
        "temporal_proximity",
        "relationship_relevance",
        "owner_relevance",
        "expectation_violation",
        "recurrence_stability",
        "source_quality",
    }
    assert all(0.0 <= value <= 1.0 for value in assessment.components.values())
    assert all(instance.id not in code for code in assessment.reason_codes)
    assert all(code == code.upper() for code in assessment.reason_codes)
