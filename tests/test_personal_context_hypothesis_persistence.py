from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.personal_context import build_personal_context
from attention_router.application.personal_context_hypotheses import (
    ContextHypothesisPersistenceError,
    PATTERN_CLAIM_PREDICATE,
    PATTERN_CLAIM_SOURCE_QUALITY,
    persist_context_pattern_hypothesis,
)
from attention_router.application.personal_context_patterns import (
    detect_temporal_recurrence_hypotheses,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    FactRow,
    MemoryClaimRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR = "owner-pattern-persistence"


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


def _install_owner(session) -> None:
    _ensure_tenant(session)
    upsert_actor_binding(
        session,
        "test",
        "owner-pattern-external",
        ACTOR,
        "owner",
        metadata={"owner": True},
        tenant_id=DEFAULT_TENANT_ID,
    )


def _event(
    session,
    *,
    occurred_at: datetime,
    provenance: str = "android-location",
) -> TimelineEventRow:
    _ensure_tenant(session)
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
        provenance=provenance,
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def _hypothesis(session, *, stamp: datetime, count: int = 3):
    start = stamp - timedelta(days=count - 1)
    for index in range(count):
        _event(
            session,
            occurred_at=start + timedelta(days=index),
            provenance=(
                "calendar"
                if index == 0
                else "android-location"
            ),
        )
    items = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=stamp,
    )
    assert len(items) == 1
    return items[0]


def test_detected_hypothesis_persists_as_governed_memory_claim(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis = _hypothesis(session, stamp=stamp)

    facts_before = session.scalar(
        select(func.count()).select_from(FactRow)
    )
    claim, changed = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    facts_after = session.scalar(
        select(func.count()).select_from(FactRow)
    )

    assert changed is True
    assert facts_after == facts_before
    assert claim.predicate == PATTERN_CLAIM_PREDICATE
    assert claim.source_quality == PATTERN_CLAIM_SOURCE_QUALITY
    assert claim.object_type == "JSON"
    assert claim.object_text is None
    assert claim.status == "ACTIVE"
    assert claim.staleness_class == "PERISHABLE"
    assert claim.confidence == hypothesis.confidence
    assert claim.object_json["evidence_class"] == "INFERRED"
    assert claim.object_json["hypothesis_status"] == "HYPOTHESIS"
    assert claim.object_json["grants_authority"] is False
    assert claim.object_json["recommendation_ready"] is False
    assert claim.context["hypothesis_id"] == hypothesis.hypothesis_id
    assert claim.context["evidence_timeline_event_ids"] == list(
        hypothesis.evidence_timeline_event_ids
    )
    assert set(claim.context["source_provenance"]) == {
        "android-location",
        "calendar",
    }

    snapshot = build_personal_context(
        session,
        DEFAULT_TENANT_ID,
        now=stamp,
    )
    persisted = [
        item
        for item in snapshot.claims
        if item.claim_id == claim.id
    ]
    assert len(persisted) == 1
    assert persisted[0].source_quality == PATTERN_CLAIM_SOURCE_QUALITY
    assert persisted[0].value_json["hypothesis_status"] == "HYPOTHESIS"


def test_exact_hypothesis_snapshot_is_idempotent(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis = _hypothesis(session, stamp=stamp)

    first, first_changed = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )
    second, second_changed = persist_context_pattern_hypothesis(
        session,
        hypothesis=hypothesis,
        now=stamp,
    )

    assert first_changed is True
    assert second_changed is False
    assert second.id == first.id
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE
        )
    ) == 1


def test_reinforced_hypothesis_supersedes_prior_snapshot_and_preserves_history(
    session,
):
    _install_owner(session)
    first_stamp = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    first_hypothesis = _hypothesis(
        session,
        stamp=first_stamp,
    )
    first, _ = persist_context_pattern_hypothesis(
        session,
        hypothesis=first_hypothesis,
        now=first_stamp,
    )

    second_stamp = first_stamp + timedelta(days=1)
    _event(
        session,
        occurred_at=second_stamp,
        provenance="android-location",
    )
    second_hypothesis = detect_temporal_recurrence_hypotheses(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id=ACTOR,
        now=second_stamp,
    )[0]

    second, changed = persist_context_pattern_hypothesis(
        session,
        hypothesis=second_hypothesis,
        now=second_stamp,
    )
    session.refresh(first)

    assert changed is True
    assert second.id != first.id
    assert second.supersedes_claim_id == first.id
    assert first.status == "SUPERSEDED"
    assert second.status == "ACTIVE"
    assert second.context["occurrence_count"] == 4
    assert second.last_observed_at == second_stamp.replace(tzinfo=None) or (
        second.last_observed_at == second_stamp
    )
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE
        )
    ) == 2

    snapshot = build_personal_context(
        session,
        DEFAULT_TENANT_ID,
        now=second_stamp,
    )
    ids = {item.claim_id for item in snapshot.claims}
    assert second.id in ids
    assert first.id not in ids


@pytest.mark.parametrize(
    "mutation,error_code",
    [
        (
            lambda item, stamp: replace(item, confidence=0.70),
            "PATTERN_HYPOTHESIS_CONFIDENCE_TOO_LOW",
        ),
        (
            lambda item, stamp: replace(item, support_ratio=0.50),
            "PATTERN_HYPOTHESIS_SUPPORT_TOO_LOW",
        ),
        (
            lambda item, stamp: replace(item, grants_authority=True),
            "PATTERN_HYPOTHESIS_AUTHORITY_FORBIDDEN",
        ),
        (
            lambda item, stamp: replace(item, recommendation_ready=True),
            "PATTERN_HYPOTHESIS_RECOMMENDATION_NOT_READY",
        ),
        (
            lambda item, stamp: replace(
                item,
                valid_until=stamp - timedelta(seconds=1),
            ),
            "PATTERN_HYPOTHESIS_EXPIRED",
        ),
    ],
)
def test_non_admissible_hypothesis_never_persists(
    session,
    mutation,
    error_code,
):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis = _hypothesis(session, stamp=stamp)

    with pytest.raises(
        ContextHypothesisPersistenceError,
        match=error_code,
    ):
        persist_context_pattern_hypothesis(
            session,
            hypothesis=mutation(hypothesis, stamp),
            now=stamp,
        )

    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE
        )
    ) == 0


def test_forged_provenance_is_rejected(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis = _hypothesis(session, stamp=stamp)
    forged = replace(
        hypothesis,
        source_provenance=("forged-source",),
    )

    with pytest.raises(
        ContextHypothesisPersistenceError,
        match="PATTERN_HYPOTHESIS_PROVENANCE_MISMATCH",
    ):
        persist_context_pattern_hypothesis(
            session,
            hypothesis=forged,
            now=stamp,
        )


def test_cross_actor_evidence_is_rejected(session):
    _install_owner(session)
    stamp = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    hypothesis = _hypothesis(session, stamp=stamp)
    forged = replace(
        hypothesis,
        actor_id="another-person",
    )

    with pytest.raises(
        ContextHypothesisPersistenceError,
        match="PATTERN_HYPOTHESIS_ACTOR_SCOPE_MISMATCH",
    ):
        persist_context_pattern_hypothesis(
            session,
            hypothesis=forged,
            now=stamp,
        )
