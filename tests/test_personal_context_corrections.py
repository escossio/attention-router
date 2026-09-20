from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from attention_router.application.personal_context_corrections import (
    ContextPatternCorrectionError,
    PATTERN_CORRECTION_PREDICATE,
    invalidate_context_pattern_hypothesis,
)
from attention_router.application.personal_context_hypotheses import (
    ContextHypothesisPersistenceError,
    persist_context_pattern_hypothesis,
)
from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure import human_identity_models as _human_identity_models  # noqa: F401
from attention_router.infrastructure.models import (
    InboundEventRow,
    MemoryClaimRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-pattern-correction"
EXTERNAL_ACTOR = "owner-pattern-correction-external"


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
        EXTERNAL_ACTOR,
        ACTOR,
        "owner",
        metadata={"owner": True},
        tenant_id=DEFAULT_TENANT_ID,
    )


def _event(session, occurred_at: datetime) -> None:
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
            occurred_at=occurred_at,
            visibility="PRIVATE",
            provenance="android-location",
            metadata_json={},
        )
    )
    session.flush()


def _persisted_hypothesis(session, stamp: datetime):
    for days in (2, 1, 0):
        _event(session, stamp - timedelta(days=days))
    hypothesis = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )[0]
    claim, _ = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    return hypothesis, claim


def _correction_event(session, stamp: datetime, *, actor_id: str = EXTERNAL_ACTOR):
    payload = {
        "actor_id": actor_id,
        "content": "isso nao e uma rotina",
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


def test_owner_correction_supersedes_inference_and_blocks_resurrection(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis, inferred = _persisted_hypothesis(session, stamp)
    correction_event = _correction_event(session, stamp + timedelta(minutes=1))

    correction, changed = invalidate_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis.hypothesis_id,
        correction_event=correction_event,
        now=stamp + timedelta(minutes=1),
    )
    session.refresh(inferred)

    assert changed is True
    assert inferred.status == "SUPERSEDED"
    assert correction.predicate == PATTERN_CORRECTION_PREDICATE
    assert correction.source_quality == "USER_DECLARED"
    assert correction.object_json["evidence_class"] == "USER_DECLARED"
    assert correction.object_json["grants_authority"] is False
    assert correction.context["corrected_claim_id"] == inferred.id
    assert correction.context["correction_event_id"] == correction_event.id

    detected_again = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp + timedelta(minutes=2),
    )[0]
    with pytest.raises(
        ContextHypothesisPersistenceError,
        match="PATTERN_HYPOTHESIS_SUPPRESSED_BY_OWNER_CORRECTION",
    ):
        persist_context_pattern_hypothesis(
            session,
            hypothesis=detected_again,
            now=stamp + timedelta(minutes=2),
        )

    active_patterns = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.predicate == "context.pattern.temporal_recurrence",
            MemoryClaimRow.status == "ACTIVE",
        )
    ).all()
    assert active_patterns == []


def test_exact_correction_event_replay_is_idempotent(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis, _ = _persisted_hypothesis(session, stamp)
    event = _correction_event(session, stamp + timedelta(minutes=1))

    first, first_changed = invalidate_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis.hypothesis_id,
        correction_event=event,
    )
    second, second_changed = invalidate_context_pattern_hypothesis(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR,
        hypothesis_id=hypothesis.hypothesis_id,
        correction_event=event,
    )

    assert first_changed is True
    assert second_changed is False
    assert second.id == first.id


def test_correction_from_unbound_actor_fails_closed(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis, inferred = _persisted_hypothesis(session, stamp)
    event = _correction_event(
        session,
        stamp + timedelta(minutes=1),
        actor_id="forged-owner",
    )

    with pytest.raises(
        ContextPatternCorrectionError,
        match="PATTERN_CORRECTION_ACTOR_MISMATCH",
    ):
        invalidate_context_pattern_hypothesis(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            actor_key=ACTOR,
            hypothesis_id=hypothesis.hypothesis_id,
            correction_event=event,
        )

    session.refresh(inferred)
    assert inferred.status == "ACTIVE"