from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context_anomalies import (
    detect_missing_step_anomalies,
)
from attention_router.application.personal_context_anomaly_hypotheses import (
    ANOMALY_CLAIM_PREDICATE,
    ContextAnomalyPersistenceError,
    persist_missing_step_anomaly,
    reconcile_missing_step_anomalies,
)
from attention_router.application.personal_context_recommendations import (
    build_context_recommendations,
)
from attention_router.application.personal_context_runtime import (
    run_personal_context_runtime_cycle,
)
from attention_router.application.personal_context_sequence_hypotheses import (
    SEQUENCE_CLAIM_PREDICATE,
    persist_context_event_sequence_hypothesis,
)
from attention_router.application.personal_context_sequences import (
    detect_event_sequence_hypotheses,
)
from attention_router.application.platform.registry import (
    sync_capability_definitions,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    FactRow,
    MemoryClaimRow,
    OutboxMessageRow,
)
from tests.test_personal_context_sequences import (
    ACTOR,
    _event,
    _install_owner,
    _three_irregular_occurrences,
)


def _persist_sequence(session, stamp: datetime):
    _three_irregular_occurrences(session, stamp)
    hypothesis = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    claim, changed = persist_context_event_sequence_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    assert changed is True
    return hypothesis, claim


def _post_learning_first_event(
    session,
    *,
    stamp: datetime,
    offset: timedelta = timedelta(hours=1),
):
    return _event(
        session,
        occurred_at=stamp + offset,
        event_type="LOCATION_DEPARTURE",
        pattern_key="home-departure",
        provenance="device-location",
    )


def _expected_second_event(
    session,
    *,
    first_at: datetime,
    gap_minutes: int = 31,
):
    return _event(
        session,
        occurred_at=first_at + timedelta(minutes=gap_minutes),
        event_type="LOCATION_ARRIVAL",
        pattern_key="gym-arrival",
        provenance="calendar",
    )


def test_closed_window_without_expected_step_creates_anomaly_candidate(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis, source = _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)

    items = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )

    assert len(items) == 1
    item = items[0]
    assert item.anomaly_type == "MISSING_EXPECTED_STEP"
    assert item.source_sequence_claim_id == source.id
    assert item.source_hypothesis_id == hypothesis.hypothesis_id
    assert item.first_event_id == first.id
    assert item.first_event_type == "LOCATION_DEPARTURE"
    assert item.expected_second_event_type == "LOCATION_ARRIVAL"
    assert item.expected_gap_seconds == 31 * 60
    assert item.window_closed_at == (
        first.occurred_at + timedelta(minutes=46)
    )
    assert item.evidence_class == "INFERRED"
    assert item.status == "HYPOTHESIS"
    assert item.grants_authority is False
    assert item.recommendation_ready is False


def test_open_window_does_not_create_missing_step_anomaly(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)

    assert detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=first.occurred_at + timedelta(minutes=40),
    ) == ()


def test_expected_step_inside_window_blocks_anomaly(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    _expected_second_event(session, first_at=first.occurred_at)

    assert detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=first.occurred_at + timedelta(minutes=47),
    ) == ()


def test_anomaly_persists_without_rewriting_source_routine_or_creating_fact(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _hypothesis, source = _persist_sequence(session, stamp)
    source_snapshot = {
        "status": source.status,
        "confidence": source.confidence,
        "context": dict(source.context or {}),
        "object_json": dict(source.object_json or {}),
    }
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    anomaly = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    facts_before = session.scalar(
        select(func.count()).select_from(FactRow)
    )

    claim, changed = persist_missing_step_anomaly(
        session,
        anomaly=anomaly,
        now=detected_at,
    )
    replay, replay_changed = persist_missing_step_anomaly(
        session,
        anomaly=anomaly,
        now=detected_at,
    )
    session.refresh(source)

    assert changed is True
    assert replay_changed is False
    assert replay.id == claim.id
    assert claim.predicate == ANOMALY_CLAIM_PREDICATE
    assert claim.source_quality == "DERIVED_PATTERN"
    assert claim.object_json["pattern_type"] == "SEQUENCE_ANOMALY"
    assert claim.object_json["anomaly_type"] == "MISSING_EXPECTED_STEP"
    assert claim.object_json["evidence_class"] == "INFERRED"
    assert claim.object_json["hypothesis_status"] == "HYPOTHESIS"
    assert claim.object_json["grants_authority"] is False
    assert claim.object_json["recommendation_ready"] is False
    assert claim.context["source_sequence_claim_id"] == source.id
    assert claim.context["first_event_id"] == first.id
    assert claim.context["detection_basis"] == (
        "ABSENCE_WITHIN_EXPECTED_WINDOW"
    )

    assert source.status == source_snapshot["status"] == "ACTIVE"
    assert source.confidence == source_snapshot["confidence"]
    assert source.context == source_snapshot["context"]
    assert source.object_json == source_snapshot["object_json"]
    assert session.scalar(
        select(func.count()).select_from(FactRow)
    ) == facts_before


def test_expected_step_arriving_between_detection_and_persistence_fails_closed(
    session,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    anomaly = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]

    _expected_second_event(session, first_at=first.occurred_at)

    with pytest.raises(
        ContextAnomalyPersistenceError,
        match="ANOMALY_EXPECTED_STEP_PRESENT",
    ):
        persist_missing_step_anomaly(
            session,
            anomaly=anomaly,
            now=detected_at,
        )


def test_late_ingested_expected_step_supersedes_absence_hypothesis(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _hypothesis, source = _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    anomaly = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    claim, _ = persist_missing_step_anomaly(
        session,
        anomaly=anomaly,
        now=detected_at,
    )

    expected = _expected_second_event(
        session,
        first_at=first.occurred_at,
    )
    reconciled = reconcile_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at + timedelta(minutes=5),
    )
    session.refresh(claim)
    session.refresh(source)

    assert reconciled == 1
    assert claim.status == "SUPERSEDED"
    assert claim.context["superseded_reason"] == (
        "EXPECTED_STEP_EVIDENCE_ARRIVED"
    )
    assert claim.context["superseding_timeline_event_id"] == expected.id
    assert source.status == "ACTIVE"


def test_inactive_source_sequence_supersedes_active_anomaly(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _hypothesis, source = _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    anomaly = detect_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    )[0]
    claim, _ = persist_missing_step_anomaly(
        session,
        anomaly=anomaly,
        now=detected_at,
    )
    source.status = "SUPERSEDED"
    session.flush()

    reconciled = reconcile_missing_step_anomalies(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at + timedelta(minutes=1),
    )
    session.refresh(claim)

    assert reconciled == 1
    assert claim.status == "SUPERSEDED"
    assert claim.context["superseded_reason"] == "SOURCE_SEQUENCE_INACTIVE"


def test_runtime_persists_anomaly_without_recommendation_or_outbox(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _persist_sequence(session, stamp)
    first = _post_learning_first_event(session, stamp=stamp)
    detected_at = first.occurred_at + timedelta(minutes=47)
    sync_capability_definitions(session)

    result = run_personal_context_runtime_cycle(
        session,
        now=detected_at,
        delivery_enabled=True,
    )

    assert result.anomaly_hypotheses_detected == 1
    assert result.anomaly_hypotheses_persisted == 1
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(MemoryClaimRow.predicate == ANOMALY_CLAIM_PREDICATE)
    ) == 1
    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=detected_at,
    ) == ()
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0
    sequence = session.scalar(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == SEQUENCE_CLAIM_PREDICATE,
            MemoryClaimRow.status == "ACTIVE",
        )
    )
    assert sequence is not None
    assert sequence.object_json["pattern_type"] == "EVENT_SEQUENCE"
