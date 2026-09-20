from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    MemoryActorRow,
    MemoryClaimRow,
    TimelineEventRow,
)


SEQUENCE_CLAIM_PREDICATE: Final = "context.pattern.event_sequence"
SEQUENCE_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
ANOMALY_TTL: Final = timedelta(days=7)
MIN_SOURCE_CONFIDENCE: Final = 0.80
MIN_SOURCE_SUPPORT_RATIO: Final = 0.75
MAX_CANDIDATE_FIRST_EVENTS: Final = 100


@dataclass(frozen=True, slots=True)
class ContextMissingStepAnomaly:
    anomaly_id: str
    tenant_id: str
    actor_id: str
    source_sequence_claim_id: str
    source_hypothesis_id: str
    first_event_id: str
    first_event_type: str
    first_signature_kind: str
    first_signature_value: str
    expected_second_event_type: str
    expected_second_signature_kind: str
    expected_second_signature_value: str
    expected_gap_seconds: int
    window_closed_at: datetime
    detected_at: datetime
    valid_until: datetime
    confidence: float
    source_sequence_confidence: float
    source_sequence_support_ratio: float
    source_sequence_occurrence_count: int
    evidence_class: str = "INFERRED"
    status: str = "HYPOTHESIS"
    anomaly_type: str = "MISSING_EXPECTED_STEP"
    grants_authority: bool = False
    recommendation_ready: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_signature(row: TimelineEventRow) -> tuple[str, str] | None:
    if row.resource_id:
        return "RESOURCE", row.resource_id
    if row.relationship_id:
        return "RELATIONSHIP", row.relationship_id
    pattern_key = (row.event_ref or {}).get("pattern_key")
    if isinstance(pattern_key, str) and pattern_key.strip():
        return "PATTERN_KEY", pattern_key.strip()
    return None


def _step_matches(
    row: TimelineEventRow,
    *,
    event_type: str,
    signature_kind: str,
    signature_value: str,
) -> bool:
    return (
        row.event_type == event_type
        and _event_signature(row) == (signature_kind, signature_value)
    )


def _gap_tolerance_seconds(expected_gap_seconds: int) -> int:
    return int(
        round(
            max(
                15 * 60,
                min(90 * 60, expected_gap_seconds * 0.25),
            )
        )
    )


def _active_sequence_claims(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime,
) -> tuple[MemoryClaimRow, ...]:
    actor_ids = session.scalars(
        select(MemoryActorRow.id).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    ).all()
    if not actor_ids:
        return ()

    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id.in_(actor_ids),
            MemoryClaimRow.predicate == SEQUENCE_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == SEQUENCE_CLAIM_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
            MemoryClaimRow.sensitivity_class != "SECRET",
            or_(
                MemoryClaimRow.valid_from.is_(None),
                MemoryClaimRow.valid_from <= now,
            ),
            or_(
                MemoryClaimRow.valid_until.is_(None),
                MemoryClaimRow.valid_until > now,
            ),
        )
        .order_by(
            MemoryClaimRow.confidence.desc(),
            MemoryClaimRow.last_observed_at.desc(),
            MemoryClaimRow.id,
        )
    ).all()
    return tuple(rows)


def _source_contract(
    claim: MemoryClaimRow,
) -> tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    float,
    int,
] | None:
    value = claim.object_json or {}
    context = claim.context or {}
    if value.get("pattern_type") != "EVENT_SEQUENCE":
        return None
    if value.get("evidence_class") != "INFERRED":
        return None
    if value.get("hypothesis_status") != "HYPOTHESIS":
        return None
    if value.get("grants_authority") is not False:
        return None
    if value.get("recommendation_ready") is not False:
        return None

    first_event_type = value.get("first_event_type")
    first_kind = value.get("first_signature_kind")
    first_value = value.get("first_signature_value")
    second_event_type = value.get("second_event_type")
    second_kind = value.get("second_signature_kind")
    second_value = value.get("second_signature_value")
    expected_gap_seconds = value.get("median_gap_seconds")
    support_ratio = context.get("support_ratio")
    occurrence_count = context.get("occurrence_count")
    if not all(
        isinstance(item, str) and item
        for item in (
            first_event_type,
            first_kind,
            first_value,
            second_event_type,
            second_kind,
            second_value,
        )
    ):
        return None
    if not isinstance(expected_gap_seconds, int) or expected_gap_seconds <= 0:
        return None
    if not isinstance(support_ratio, (int, float)):
        return None
    if not isinstance(occurrence_count, int):
        return None
    if claim.confidence < MIN_SOURCE_CONFIDENCE:
        return None
    if float(support_ratio) < MIN_SOURCE_SUPPORT_RATIO:
        return None
    if occurrence_count < 3:
        return None
    return (
        first_event_type,
        first_kind,
        first_value,
        second_event_type,
        second_kind,
        second_value,
        expected_gap_seconds,
        float(support_ratio),
        occurrence_count,
    )


def detect_missing_step_anomalies(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
    result_limit: int = 20,
) -> tuple[ContextMissingStepAnomaly, ...]:
    """Detect overdue missing second steps from active learned sequences.

    One missing step is anomaly evidence only. It never rewrites the source
    routine, creates a fact, or grants execution/disclosure authority.
    """

    if not actor_key.strip():
        return ()
    if result_limit < 1 or result_limit > 50:
        raise ValueError("ANOMALY_RESULT_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now or datetime.now(UTC))
    anomalies: list[ContextMissingStepAnomaly] = []

    for source in _active_sequence_claims(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        now=stamp,
    ):
        contract = _source_contract(source)
        if contract is None or source.last_observed_at is None:
            continue

        (
            first_event_type,
            first_kind,
            first_value,
            second_event_type,
            second_kind,
            second_value,
            expected_gap_seconds,
            support_ratio,
            occurrence_count,
        ) = contract

        hypothesis_id = (source.context or {}).get("hypothesis_id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            continue

        tolerance_seconds = _gap_tolerance_seconds(expected_gap_seconds)
        first_rows = session.scalars(
            select(TimelineEventRow)
            .where(
                TimelineEventRow.tenant_id == tenant_id,
                TimelineEventRow.actor_id == actor_key,
                TimelineEventRow.occurred_at > source.last_observed_at,
                TimelineEventRow.occurred_at <= stamp,
                TimelineEventRow.visibility != "SECRET",
            )
            .order_by(
                TimelineEventRow.occurred_at,
                TimelineEventRow.id,
            )
            .limit(MAX_CANDIDATE_FIRST_EVENTS)
        ).all()

        for first in first_rows:
            if not _step_matches(
                first,
                event_type=first_event_type,
                signature_kind=first_kind,
                signature_value=first_value,
            ):
                continue

            window_closed_at = _utc(first.occurred_at) + timedelta(
                seconds=expected_gap_seconds + tolerance_seconds
            )
            if stamp <= window_closed_at:
                continue

            candidates = session.scalars(
                select(TimelineEventRow)
                .where(
                    TimelineEventRow.tenant_id == tenant_id,
                    TimelineEventRow.actor_id == actor_key,
                    TimelineEventRow.occurred_at > first.occurred_at,
                    TimelineEventRow.occurred_at <= window_closed_at,
                    TimelineEventRow.visibility != "SECRET",
                )
                .order_by(
                    TimelineEventRow.occurred_at,
                    TimelineEventRow.id,
                )
            ).all()
            if any(
                _step_matches(
                    row,
                    event_type=second_event_type,
                    signature_kind=second_kind,
                    signature_value=second_value,
                )
                for row in candidates
            ):
                continue

            anomaly_id = "anomaly-" + stable_hash(
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_key,
                    "source_sequence_claim_id": source.id,
                    "source_hypothesis_id": hypothesis_id,
                    "first_event_id": first.id,
                    "anomaly_type": "MISSING_EXPECTED_STEP",
                }
            )[:32]
            confidence = round(
                min(0.90, max(0.0, source.confidence * support_ratio * 0.95)),
                3,
            )
            anomalies.append(
                ContextMissingStepAnomaly(
                    anomaly_id=anomaly_id,
                    tenant_id=tenant_id,
                    actor_id=actor_key,
                    source_sequence_claim_id=source.id,
                    source_hypothesis_id=hypothesis_id,
                    first_event_id=first.id,
                    first_event_type=first_event_type,
                    first_signature_kind=first_kind,
                    first_signature_value=first_value,
                    expected_second_event_type=second_event_type,
                    expected_second_signature_kind=second_kind,
                    expected_second_signature_value=second_value,
                    expected_gap_seconds=expected_gap_seconds,
                    window_closed_at=window_closed_at,
                    detected_at=stamp,
                    valid_until=stamp + ANOMALY_TTL,
                    confidence=confidence,
                    source_sequence_confidence=source.confidence,
                    source_sequence_support_ratio=support_ratio,
                    source_sequence_occurrence_count=occurrence_count,
                )
            )

    anomalies.sort(
        key=lambda item: (
            -item.detected_at.timestamp(),
            item.anomaly_id,
        )
    )
    return tuple(anomalies[:result_limit])


__all__ = [
    "ANOMALY_TTL",
    "ContextMissingStepAnomaly",
    "MIN_SOURCE_CONFIDENCE",
    "MIN_SOURCE_SUPPORT_RATIO",
    "detect_missing_step_anomalies",
]
