from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import TimelineEventRow


MIN_SEQUENCE_GAP: Final = timedelta(minutes=1)
MAX_SEQUENCE_GAP: Final = timedelta(hours=6)
MAX_SEQUENCE_EVENTS: Final = 300
DEFAULT_SEQUENCE_TTL: Final = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class ContextEventSequenceHypothesis:
    hypothesis_id: str
    tenant_id: str
    actor_id: str
    pattern_type: str
    first_event_type: str
    first_signature_kind: str
    first_signature_value: str
    second_event_type: str
    second_signature_kind: str
    second_signature_value: str
    evidence_class: str
    status: str
    confidence: float
    median_gap_seconds: int
    occurrence_count: int
    anomaly_count: int
    support_ratio: float
    first_observed_at: datetime
    last_observed_at: datetime
    valid_until: datetime
    evidence_transition_pairs: tuple[tuple[str, str], ...]
    source_provenance: tuple[str, ...]
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


def _step(row: TimelineEventRow) -> tuple[str, str, str] | None:
    signature = _event_signature(row)
    if signature is None:
        return None
    return row.event_type, signature[0], signature[1]


def _gap_tolerance_seconds(median_gap_seconds: float) -> float:
    return max(
        15 * 60,
        min(90 * 60, median_gap_seconds * 0.25),
    )


def _confidence(*, occurrence_count: int, support_ratio: float) -> float:
    occurrence_bonus = min(0.15, max(0, occurrence_count - 3) * 0.05)
    return round(
        min(0.95, 0.65 + 0.15 * support_ratio + occurrence_bonus),
        3,
    )


def detect_event_sequence_hypotheses(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    now: datetime | None = None,
    lookback_days: int = 60,
    event_limit: int = MAX_SEQUENCE_EVENTS,
    result_limit: int = 20,
) -> tuple[ContextEventSequenceHypothesis, ...]:
    """Detect bounded two-step routines from adjacent canonical timeline events.

    Only adjacent scoped events are considered. This deliberately trades recall
    for explainability and avoids unrestricted all-pairs correlation.
    """

    if not actor_id.strip():
        return ()
    if lookback_days < 1 or lookback_days > 365:
        raise ValueError("SEQUENCE_LOOKBACK_OUT_OF_RANGE")
    if event_limit < 6 or event_limit > 500:
        raise ValueError("SEQUENCE_EVENT_LIMIT_OUT_OF_RANGE")
    if result_limit < 1 or result_limit > 50:
        raise ValueError("SEQUENCE_RESULT_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now or datetime.now(UTC))
    window_start = stamp - timedelta(days=lookback_days)
    rows = list(
        session.scalars(
            select(TimelineEventRow)
            .where(
                TimelineEventRow.tenant_id == tenant_id,
                TimelineEventRow.actor_id == actor_id,
                TimelineEventRow.occurred_at >= window_start,
                TimelineEventRow.occurred_at <= stamp,
                TimelineEventRow.visibility != "SECRET",
            )
            .order_by(
                TimelineEventRow.occurred_at.desc(),
                TimelineEventRow.id.desc(),
            )
            .limit(event_limit)
        ).all()
    )
    ordered = sorted(
        rows,
        key=lambda item: (_utc(item.occurred_at), item.id),
    )

    grouped: dict[
        tuple[str, str, str, str, str, str],
        list[tuple[TimelineEventRow, TimelineEventRow, float]],
    ] = {}
    for left, right in zip(ordered, ordered[1:], strict=False):
        left_step = _step(left)
        right_step = _step(right)
        if left_step is None or right_step is None:
            continue
        if left_step == right_step:
            continue
        gap_seconds = (
            _utc(right.occurred_at) - _utc(left.occurred_at)
        ).total_seconds()
        if not (
            MIN_SEQUENCE_GAP.total_seconds()
            <= gap_seconds
            <= MAX_SEQUENCE_GAP.total_seconds()
        ):
            continue
        key = (*left_step, *right_step)
        grouped.setdefault(key, []).append((left, right, gap_seconds))

    hypotheses: list[ContextEventSequenceHypothesis] = []
    for key, transitions in grouped.items():
        if len(transitions) < 3:
            continue

        median_gap = float(median(item[2] for item in transitions))
        tolerance = _gap_tolerance_seconds(median_gap)
        stable = [
            item
            for item in transitions
            if abs(item[2] - median_gap) <= tolerance
        ]
        if len(stable) < 3:
            continue

        support_ratio = round(len(stable) / len(transitions), 3)
        first_event_type, first_kind, first_value, second_event_type, second_kind, second_value = key
        first_observed_at = min(_utc(item[0].occurred_at) for item in stable)
        last_observed_at = max(_utc(item[1].occurred_at) for item in stable)
        valid_until = last_observed_at + DEFAULT_SEQUENCE_TTL
        if valid_until <= stamp:
            continue

        hypothesis_id = "sequence-" + stable_hash(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pattern_type": "EVENT_SEQUENCE",
                "first": {
                    "event_type": first_event_type,
                    "signature_kind": first_kind,
                    "signature_value": first_value,
                },
                "second": {
                    "event_type": second_event_type,
                    "signature_kind": second_kind,
                    "signature_value": second_value,
                },
            }
        )[:32]
        provenance = tuple(
            sorted(
                {
                    row.provenance
                    for left, right, _gap in stable
                    for row in (left, right)
                }
            )
        )
        hypotheses.append(
            ContextEventSequenceHypothesis(
                hypothesis_id=hypothesis_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                pattern_type="EVENT_SEQUENCE",
                first_event_type=first_event_type,
                first_signature_kind=first_kind,
                first_signature_value=first_value,
                second_event_type=second_event_type,
                second_signature_kind=second_kind,
                second_signature_value=second_value,
                evidence_class="INFERRED",
                status="HYPOTHESIS",
                confidence=_confidence(
                    occurrence_count=len(stable),
                    support_ratio=support_ratio,
                ),
                median_gap_seconds=int(round(median_gap)),
                occurrence_count=len(stable),
                anomaly_count=len(transitions) - len(stable),
                support_ratio=support_ratio,
                first_observed_at=first_observed_at,
                last_observed_at=last_observed_at,
                valid_until=valid_until,
                evidence_transition_pairs=tuple(
                    (left.id, right.id)
                    for left, right, _gap in stable
                ),
                source_provenance=provenance,
                grants_authority=False,
                recommendation_ready=False,
            )
        )

    hypotheses.sort(
        key=lambda item: (
            -item.confidence,
            -item.occurrence_count,
            -item.last_observed_at.timestamp(),
            item.hypothesis_id,
        )
    )
    return tuple(hypotheses[:result_limit])


__all__ = [
    "ContextEventSequenceHypothesis",
    "DEFAULT_SEQUENCE_TTL",
    "MAX_SEQUENCE_GAP",
    "MIN_SEQUENCE_GAP",
    "detect_event_sequence_hypotheses",
]
