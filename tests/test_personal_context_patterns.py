from __future__ import annotations

from datetime import UTC, datetime, timedelta

from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import TenantRow, TimelineEventRow


ACTOR = "owner-pattern-actor"


def _ensure_tenant(session, tenant_id: str) -> None:
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


def _event(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    actor_id: str = ACTOR,
    occurred_at: datetime,
    pattern_key: str | None = "gym-arrival",
    event_type: str = "LOCATION_ARRIVAL",
    provenance: str = "android-location",
) -> TimelineEventRow:
    _ensure_tenant(session, tenant_id)
    row = TimelineEventRow(
        id=new_id(),
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=actor_id,
        relationship_id=None,
        resource_id=None,
        event_type=event_type,
        event_ref=(
            {"pattern_key": pattern_key}
            if pattern_key is not None
            else {}
        ),
        occurred_at=occurred_at,
        visibility="PRIVATE",
        provenance=provenance,
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def test_three_stable_occurrences_create_bounded_recurrence_hypothesis(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    first = _event(
        session,
        occurred_at=stamp - timedelta(days=2),
        provenance="calendar",
    )
    second = _event(
        session,
        occurred_at=stamp - timedelta(days=1),
        provenance="android-location",
    )
    third = _event(
        session,
        occurred_at=stamp,
        provenance="android-location",
    )

    hypotheses = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )

    assert len(hypotheses) == 1
    hypothesis = hypotheses[0]
    assert hypothesis.pattern_type == "TEMPORAL_RECURRENCE"
    assert hypothesis.evidence_class == "INFERRED"
    assert hypothesis.status == "HYPOTHESIS"
    assert hypothesis.signature_kind == "PATTERN_KEY"
    assert hypothesis.signature_value == "gym-arrival"
    assert hypothesis.event_type == "LOCATION_ARRIVAL"
    assert hypothesis.cadence_seconds == 24 * 60 * 60
    assert hypothesis.occurrence_count == 3
    assert hypothesis.anomaly_count == 0
    assert hypothesis.support_ratio == 1.0
    assert hypothesis.confidence >= 0.8
    assert hypothesis.evidence_timeline_event_ids == (
        first.id,
        second.id,
        third.id,
    )
    assert hypothesis.source_provenance == (
        "android-location",
        "calendar",
    )
    assert hypothesis.valid_until > stamp
    assert hypothesis.grants_authority is False
    assert hypothesis.recommendation_ready is False


def test_two_occurrences_do_not_create_pattern_hypothesis(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _event(session, occurred_at=stamp - timedelta(days=1))
    _event(session, occurred_at=stamp)

    hypotheses = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )

    assert hypotheses == ()


def test_tenant_and_actor_evidence_never_mix(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    tenant_b = "00000000-0000-4000-8000-0000000000c2"

    _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        occurred_at=stamp - timedelta(days=1),
    )
    _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        occurred_at=stamp,
    )
    _event(
        session,
        tenant_id=tenant_b,
        actor_id=ACTOR,
        occurred_at=stamp - timedelta(days=2),
    )
    _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id="other-person",
        occurred_at=stamp - timedelta(days=2),
    )

    assert detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    ) == ()


def test_one_anomaly_does_not_rewrite_stable_daily_recurrence(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    base = stamp - timedelta(days=3)

    for day in range(4):
        _event(
            session,
            occurred_at=base + timedelta(days=day),
            provenance="android-location",
        )
    _event(
        session,
        occurred_at=base + timedelta(days=1, hours=6),
        provenance="manual-observation",
    )

    hypotheses = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )

    assert len(hypotheses) == 1
    hypothesis = hypotheses[0]
    assert hypothesis.cadence_seconds == 24 * 60 * 60
    assert hypothesis.occurrence_count == 4
    assert hypothesis.anomaly_count == 1
    assert hypothesis.support_ratio == 0.8


def test_stale_recurrence_expires_and_is_not_returned(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    base = stamp - timedelta(days=20)

    for day in range(3):
        _event(
            session,
            occurred_at=base + timedelta(days=day),
        )

    hypotheses = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
        lookback_days=60,
    )

    assert hypotheses == ()


def test_unscoped_repeated_events_are_not_generalized_into_patterns(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    for day in range(3):
        _event(
            session,
            occurred_at=stamp - timedelta(days=2 - day),
            pattern_key=None,
            event_type="MESSAGE_RECEIVED",
        )

    hypotheses = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )

    assert hypotheses == ()
