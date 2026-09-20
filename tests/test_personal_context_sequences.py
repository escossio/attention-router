from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context_corrections import (
    invalidate_context_pattern_hypothesis,
)
from attention_router.application.personal_context_runtime import (
    run_personal_context_runtime_cycle,
)
from attention_router.application.personal_context_recommendations import (
    build_context_recommendations,
)
from attention_router.application.platform.registry import (
    sync_capability_definitions,
)
from attention_router.application.personal_context_sequence_hypotheses import (
    ContextSequencePersistenceError,
    SEQUENCE_CLAIM_PREDICATE,
    persist_context_event_sequence_hypothesis,
)
from attention_router.application.personal_context_sequences import (
    detect_event_sequence_hypotheses,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    InboundEventRow,
    MemoryClaimRow,
    OutboxMessageRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-sequence"
EXTERNAL = "owner-sequence@c.us"


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


def _event(
    session,
    *,
    occurred_at: datetime,
    event_type: str,
    pattern_key: str,
    provenance: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    actor_id: str = ACTOR,
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
        event_ref={"pattern_key": pattern_key},
        occurred_at=occurred_at,
        visibility="PRIVATE",
        provenance=provenance,
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def _sequence_occurrence(
    session,
    *,
    start: datetime,
    gap_minutes: int = 30,
    first_provenance: str = "android-location",
    second_provenance: str = "calendar",
):
    left = _event(
        session,
        occurred_at=start,
        event_type="LOCATION_DEPARTURE",
        pattern_key="home-departure",
        provenance=first_provenance,
    )
    right = _event(
        session,
        occurred_at=start + timedelta(minutes=gap_minutes),
        event_type="LOCATION_ARRIVAL",
        pattern_key="gym-arrival",
        provenance=second_provenance,
    )
    return left, right


def _three_irregular_occurrences(session, stamp: datetime):
    pairs = [
        _sequence_occurrence(
            session,
            start=stamp - timedelta(days=5),
            gap_minutes=30,
            first_provenance="android-location",
            second_provenance="calendar",
        ),
        _sequence_occurrence(
            session,
            start=stamp - timedelta(days=3),
            gap_minutes=32,
            first_provenance="android-location",
            second_provenance="sms",
        ),
        _sequence_occurrence(
            session,
            start=stamp - timedelta(minutes=31),
            gap_minutes=31,
            first_provenance="device-location",
            second_provenance="calendar",
        ),
    ]
    return pairs


def _install_owner(session) -> None:
    _ensure_tenant(session)
    upsert_actor_binding(
        session,
        "wwebjs",
        EXTERNAL,
        ACTOR,
        "owner",
        metadata={"owner": True, "owner_channel_role": "PRIMARY_OWNER_WHATSAPP"},
        tenant_id=DEFAULT_TENANT_ID,
    )


def _correction_event(session, stamp: datetime) -> InboundEventRow:
    payload = {
        "actor_id": EXTERNAL,
        "content": "isso não é uma rotina",
        "event_origin": "OWNER_COMMAND",
        "owner_authenticated": True,
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
        external_event_id=new_id(),
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


def test_three_irregular_repetitions_create_bounded_two_step_sequence(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    pairs = _three_irregular_occurrences(session, stamp)

    items = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )

    assert len(items) == 1
    item = items[0]
    assert item.pattern_type == "EVENT_SEQUENCE"
    assert item.first_event_type == "LOCATION_DEPARTURE"
    assert item.first_signature_kind == "PATTERN_KEY"
    assert item.first_signature_value == "home-departure"
    assert item.second_event_type == "LOCATION_ARRIVAL"
    assert item.second_signature_kind == "PATTERN_KEY"
    assert item.second_signature_value == "gym-arrival"
    assert item.occurrence_count == 3
    assert item.anomaly_count == 0
    assert item.support_ratio == 1.0
    assert item.median_gap_seconds == 31 * 60
    assert item.confidence >= 0.8
    assert item.evidence_transition_pairs == tuple(
        (left.id, right.id)
        for left, right in pairs
    )
    assert item.source_provenance == (
        "android-location",
        "calendar",
        "device-location",
        "sms",
    )
    assert item.grants_authority is False
    assert item.recommendation_ready is False


def test_two_sequence_occurrences_are_insufficient(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _sequence_occurrence(session, start=stamp - timedelta(days=3))
    _sequence_occurrence(session, start=stamp - timedelta(days=1))

    assert detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    ) == ()


def test_sequence_does_not_mix_tenant_or_actor_evidence(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _sequence_occurrence(session, start=stamp - timedelta(days=5))
    _sequence_occurrence(session, start=stamp - timedelta(days=3))

    other_tenant = "sequence-other-tenant"
    _event(
        session,
        occurred_at=stamp - timedelta(minutes=30),
        event_type="LOCATION_DEPARTURE",
        pattern_key="home-departure",
        provenance="android-location",
        tenant_id=other_tenant,
    )
    _event(
        session,
        occurred_at=stamp,
        event_type="LOCATION_ARRIVAL",
        pattern_key="gym-arrival",
        provenance="calendar",
        tenant_id=other_tenant,
    )
    _event(
        session,
        occurred_at=stamp - timedelta(minutes=30),
        event_type="LOCATION_DEPARTURE",
        pattern_key="home-departure",
        provenance="android-location",
        actor_id="other-owner",
    )
    _event(
        session,
        occurred_at=stamp,
        event_type="LOCATION_ARRIVAL",
        pattern_key="gym-arrival",
        provenance="calendar",
        actor_id="other-owner",
    )

    assert detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    ) == ()


def test_sequence_hypothesis_persists_as_inferred_non_authoritative_claim(session):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _three_irregular_occurrences(session, stamp)
    hypothesis = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]

    first, changed = persist_context_event_sequence_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    second, replay_changed = persist_context_event_sequence_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )

    assert changed is True
    assert replay_changed is False
    assert second.id == first.id
    assert first.predicate == SEQUENCE_CLAIM_PREDICATE
    assert first.source_quality == "DERIVED_PATTERN"
    assert first.object_json["pattern_type"] == "EVENT_SEQUENCE"
    assert first.object_json["evidence_class"] == "INFERRED"
    assert first.object_json["hypothesis_status"] == "HYPOTHESIS"
    assert first.object_json["grants_authority"] is False
    assert first.object_json["recommendation_ready"] is False
    assert first.context["occurrence_count"] == 3


def test_owner_correction_invalidates_sequence_and_blocks_old_evidence_revival(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _three_irregular_occurrences(session, stamp)
    hypothesis = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    claim, _ = persist_context_event_sequence_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    event = _correction_event(session, stamp + timedelta(minutes=1))

    correction, changed = invalidate_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis.hypothesis_id,
        correction_event=event,
        now=stamp + timedelta(minutes=1),
    )
    session.refresh(claim)

    assert changed is True
    assert claim.status == "SUPERSEDED"
    assert correction.context["corrected_predicate"] == SEQUENCE_CLAIM_PREDICATE
    assert correction.object_json["corrected_pattern_type"] == "EVENT_SEQUENCE"

    detected_again = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp + timedelta(minutes=2),
    )[0]
    with pytest.raises(
        ContextSequencePersistenceError,
        match="SEQUENCE_HYPOTHESIS_SUPPRESSED_BY_OWNER_CORRECTION",
    ):
        persist_context_event_sequence_hypothesis(
            session,
            hypothesis=detected_again,
            now=stamp + timedelta(minutes=2),
        )


def test_runtime_persists_sequence_without_creating_actionable_recommendation(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _three_irregular_occurrences(session, stamp)

    result = run_personal_context_runtime_cycle(
        session,
        now=stamp,
        delivery_enabled=True,
    )

    assert result.sequence_hypotheses_detected == 1
    assert result.sequence_hypotheses_persisted == 1
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(MemoryClaimRow.predicate == SEQUENCE_CLAIM_PREDICATE)
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(MemoryClaimRow.predicate == "context.recommendation.proactive")
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0


def test_sequence_claim_stays_non_actionable_even_when_reminder_capability_exists(
    session,
):
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    _three_irregular_occurrences(session, stamp)
    hypothesis = detect_event_sequence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    persist_context_event_sequence_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    sync_capability_definitions(session)

    assert build_context_recommendations(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        now=stamp,
    ) == ()
