from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_patterns import (
    ContextPatternHypothesis,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    MemoryActorRow,
    MemoryClaimRow,
    TimelineEventRow,
)


PATTERN_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
PATTERN_CLAIM_PREDICATE: Final = "context.pattern.temporal_recurrence"
MIN_PERSISTED_PATTERN_CONFIDENCE: Final = 0.75
MIN_PERSISTED_PATTERN_SUPPORT_RATIO: Final = 0.60


class ContextHypothesisPersistenceError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _memory_actor(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> MemoryActorRow:
    row = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if row is not None:
        return row

    stamp = now_utc()
    row = MemoryActorRow(
        id=new_id(),
        tenant_id=tenant_id,
        actor_key=actor_key,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _timeline_signature(row: TimelineEventRow) -> tuple[str, str] | None:
    if row.resource_id:
        return "RESOURCE", row.resource_id
    pattern_key = (row.event_ref or {}).get("pattern_key")
    if isinstance(pattern_key, str) and pattern_key.strip():
        return "PATTERN_KEY", pattern_key.strip()
    return None


def _validated_timeline_evidence(
    session: Session,
    *,
    hypothesis: ContextPatternHypothesis,
) -> tuple[TimelineEventRow, ...]:
    ids = tuple(dict.fromkeys(hypothesis.evidence_timeline_event_ids))
    if len(ids) != len(hypothesis.evidence_timeline_event_ids):
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_DUPLICATE_EVIDENCE"
        )
    if len(ids) < 3:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_INSUFFICIENT_EVIDENCE"
        )

    rows = session.scalars(
        select(TimelineEventRow).where(TimelineEventRow.id.in_(ids))
    ).all()
    by_id = {row.id: row for row in rows}
    if set(by_id) != set(ids):
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_EVIDENCE_NOT_FOUND"
        )

    ordered: list[TimelineEventRow] = []
    for event_id in ids:
        row = by_id[event_id]
        if row.tenant_id != hypothesis.tenant_id:
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_TENANT_SCOPE_MISMATCH"
            )
        if row.actor_id != hypothesis.actor_id:
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_ACTOR_SCOPE_MISMATCH"
            )
        if row.event_type != hypothesis.event_type:
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_EVENT_TYPE_MISMATCH"
            )
        if row.visibility == "SECRET":
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_SECRET_EVIDENCE_FORBIDDEN"
            )
        signature = _timeline_signature(row)
        if signature != (
            hypothesis.signature_kind,
            hypothesis.signature_value,
        ):
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_SIGNATURE_MISMATCH"
            )
        ordered.append(row)

    actual_provenance = {row.provenance for row in ordered}
    if actual_provenance != set(hypothesis.source_provenance):
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_PROVENANCE_MISMATCH"
        )

    return tuple(ordered)


def _assert_admissible(
    hypothesis: ContextPatternHypothesis,
    *,
    now: datetime,
) -> None:
    if hypothesis.pattern_type != "TEMPORAL_RECURRENCE":
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_TYPE_UNSUPPORTED"
        )
    if hypothesis.evidence_class != "INFERRED":
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_EVIDENCE_CLASS_INVALID"
        )
    if hypothesis.status != "HYPOTHESIS":
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_STATUS_INVALID"
        )
    if hypothesis.grants_authority:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_AUTHORITY_FORBIDDEN"
        )
    if hypothesis.recommendation_ready:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_RECOMMENDATION_NOT_READY"
        )
    if hypothesis.confidence < MIN_PERSISTED_PATTERN_CONFIDENCE:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_CONFIDENCE_TOO_LOW"
        )
    if hypothesis.support_ratio < MIN_PERSISTED_PATTERN_SUPPORT_RATIO:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_SUPPORT_TOO_LOW"
        )
    if hypothesis.occurrence_count < 3:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_OCCURRENCE_COUNT_INVALID"
        )
    if _utc(hypothesis.valid_until) <= _utc(now):
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_EXPIRED"
        )


def _snapshot_fingerprint(
    hypothesis: ContextPatternHypothesis,
) -> str:
    return stable_hash(
        {
            "hypothesis_id": hypothesis.hypothesis_id,
            "pattern_type": hypothesis.pattern_type,
            "event_type": hypothesis.event_type,
            "signature_kind": hypothesis.signature_kind,
            "signature_value": hypothesis.signature_value,
            "confidence": hypothesis.confidence,
            "cadence_seconds": hypothesis.cadence_seconds,
            "occurrence_count": hypothesis.occurrence_count,
            "anomaly_count": hypothesis.anomaly_count,
            "support_ratio": hypothesis.support_ratio,
            "first_observed_at": _utc(hypothesis.first_observed_at).isoformat(),
            "last_observed_at": _utc(hypothesis.last_observed_at).isoformat(),
            "valid_until": _utc(hypothesis.valid_until).isoformat(),
            "evidence_timeline_event_ids": list(
                hypothesis.evidence_timeline_event_ids
            ),
            "source_provenance": list(hypothesis.source_provenance),
        }
    )


def _active_same_hypothesis(
    session: Session,
    *,
    actor_id: str,
    hypothesis_id: str,
) -> list[MemoryClaimRow]:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE,
            MemoryClaimRow.status == "ACTIVE",
            MemoryClaimRow.source_quality == PATTERN_CLAIM_SOURCE_QUALITY,
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    return [
        row
        for row in rows
        if (row.context or {}).get("hypothesis_id") == hypothesis_id
    ]


def persist_context_pattern_hypothesis(
    session: Session,
    *,
    hypothesis: ContextPatternHypothesis,
    now: datetime | None = None,
) -> tuple[MemoryClaimRow, bool]:
    """Persist one governed pattern hypothesis into Personal Context.

    The claim remains explicitly inferred and non-authoritative. Replaying the
    exact same hypothesis snapshot is idempotent. A changed snapshot supersedes
    the previous active claim while preserving history.
    """

    stamp = _utc(now or datetime.now(UTC))
    _assert_admissible(hypothesis, now=stamp)
    evidence = _validated_timeline_evidence(
        session,
        hypothesis=hypothesis,
    )
    actor = _memory_actor(
        session,
        tenant_id=hypothesis.tenant_id,
        actor_key=hypothesis.actor_id,
    )
    corrections = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == "context.pattern.owner_correction",
            MemoryClaimRow.source_quality == "USER_DECLARED",
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    active_corrections = [
        row
        for row in corrections
        if (row.context or {}).get("hypothesis_id") == hypothesis.hypothesis_id
        and row.valid_until is not None
        and _utc(row.valid_until) > stamp
    ]
    if len(active_corrections) > 1:
        raise ContextHypothesisPersistenceError(
            "PATTERN_CORRECTION_ACTIVE_CONFLICT"
        )
    if active_corrections:
        correction = active_corrections[0]
        correction_at = _utc(
            correction.valid_from
            or correction.first_observed_at
            or stamp
        )
        post_correction_evidence = sum(
            _utc(row.occurred_at) > correction_at
            for row in evidence
        )
        if post_correction_evidence < 3:
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_SUPPRESSED_BY_OWNER_CORRECTION"
            )
        correction.status = "SUPERSEDED"
        correction.valid_until = min(
            _utc(correction.valid_until),
            stamp,
        )
        correction.updated_at = now_utc()

    active = _active_same_hypothesis(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis.hypothesis_id,
    )
    if len(active) > 1:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_ACTIVE_CONFLICT"
        )

    fingerprint = _snapshot_fingerprint(hypothesis)
    previous = active[0] if active else None
    if (
        previous is not None
        and (previous.context or {}).get("snapshot_fingerprint")
        == fingerprint
    ):
        return previous, False

    if previous is not None:
        previous.status = "SUPERSEDED"
        replacement_time = _utc(hypothesis.last_observed_at)
        previous.valid_until = (
            min(_utc(previous.valid_until), replacement_time)
            if previous.valid_until is not None
            else replacement_time
        )
        previous.updated_at = now_utc()

    claim = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=PATTERN_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": hypothesis.pattern_type,
            "event_type": hypothesis.event_type,
            "signature_kind": hypothesis.signature_kind,
            "signature_value": hypothesis.signature_value,
            "cadence_seconds": hypothesis.cadence_seconds,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": hypothesis.hypothesis_id,
            "snapshot_fingerprint": fingerprint,
            "evidence_timeline_event_ids": [
                row.id for row in evidence
            ],
            "source_provenance": list(hypothesis.source_provenance),
            "occurrence_count": hypothesis.occurrence_count,
            "anomaly_count": hypothesis.anomaly_count,
            "support_ratio": hypothesis.support_ratio,
        },
        confidence=hypothesis.confidence,
        sensitivity_class="PRIVATE",
        source_quality=PATTERN_CLAIM_SOURCE_QUALITY,
        valid_from=_utc(hypothesis.first_observed_at),
        valid_until=_utc(hypothesis.valid_until),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=previous.id if previous is not None else None,
        conflict_group_id=None,
        first_observed_at=_utc(hypothesis.first_observed_at),
        last_observed_at=_utc(hypothesis.last_observed_at),
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(claim)
    session.flush()
    return claim, True


__all__ = [
    "ContextHypothesisPersistenceError",
    "MIN_PERSISTED_PATTERN_CONFIDENCE",
    "MIN_PERSISTED_PATTERN_SUPPORT_RATIO",
    "PATTERN_CLAIM_PREDICATE",
    "PATTERN_CLAIM_SOURCE_QUALITY",
    "persist_context_pattern_hypothesis",
]