from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import TimelineEventRow


MIN_CADENCE: Final = timedelta(hours=1)
MAX_CADENCE: Final = timedelta(days=30)
MAX_GROUP_EVENTS: Final = 40
DEFAULT_HYPOTHESIS_TTL_MIN: Final = timedelta(days=3)
DEFAULT_HYPOTHESIS_TTL_MAX: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class ContextPatternHypothesis:
    hypothesis_id: str
    tenant_id: str
    actor_id: str
    pattern_type: str
    event_type: str
    signature_kind: str
    signature_value: str
    evidence_class: str
    status: str
    confidence: float
    cadence_seconds: int
    occurrence_count: int
    anomaly_count: int
    support_ratio: float
    first_observed_at: datetime
    last_observed_at: datetime
    valid_until: datetime
    evidence_timeline_event_ids: tuple[str, ...]
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


def _tolerance_seconds(cadence_seconds: float) -> float:
    return max(
        15 * 60,
        min(6 * 60 * 60, cadence_seconds * 0.15),
    )


def _candidate_cadences(rows: list[TimelineEventRow]) -> tuple[float, ...]:
    values: set[int] = set()
    minimum = MIN_CADENCE.total_seconds()
    maximum = MAX_CADENCE.total_seconds()
    for left_index, left in enumerate(rows):
        left_stamp = _utc(left.occurred_at)
        for right in rows[left_index + 1 :]:
            delta = (_utc(right.occurred_at) - left_stamp).total_seconds()
            if minimum <= delta <= maximum:
                values.add(int(round(delta)))
    return tuple(sorted(float(value) for value in values))


def _chain_for(
    rows: list[TimelineEventRow],
    *,
    start_index: int,
    cadence_seconds: float,
) -> tuple[list[TimelineEventRow], float]:
    tolerance = _tolerance_seconds(cadence_seconds)
    chain = [rows[start_index]]
    errors: list[float] = []
    current_index = start_index

    while current_index < len(rows) - 1:
        target = _utc(chain[-1].occurred_at) + timedelta(seconds=cadence_seconds)
        best_index: int | None = None
        best_error: float | None = None
        for candidate_index in range(current_index + 1, len(rows)):
            candidate_stamp = _utc(rows[candidate_index].occurred_at)
            error = abs((candidate_stamp - target).total_seconds())
            if candidate_stamp > target + timedelta(seconds=tolerance):
                break
            if error <= tolerance and (best_error is None or error < best_error):
                best_index = candidate_index
                best_error = error
        if best_index is None or best_error is None:
            break
        chain.append(rows[best_index])
        errors.append(best_error)
        current_index = best_index

    error_ratio = (
        mean(errors) / cadence_seconds
        if errors and cadence_seconds > 0
        else 0.0
    )
    return chain, error_ratio


def _best_recurrence_chain(
    rows: list[TimelineEventRow],
) -> tuple[list[TimelineEventRow], float, float] | None:
    if len(rows) < 3:
        return None

    best: tuple[list[TimelineEventRow], float, float] | None = None
    for cadence_seconds in _candidate_cadences(rows):
        for start_index in range(len(rows) - 2):
            chain, error_ratio = _chain_for(
                rows,
                start_index=start_index,
                cadence_seconds=cadence_seconds,
            )
            if len(chain) < 3:
                continue
            if best is None:
                best = (chain, cadence_seconds, error_ratio)
                continue
            best_chain, best_cadence, best_error = best
            score = (
                len(chain),
                -error_ratio,
                -cadence_seconds,
            )
            best_score = (
                len(best_chain),
                -best_error,
                -best_cadence,
            )
            if score > best_score:
                best = (chain, cadence_seconds, error_ratio)
    return best


def _hypothesis_valid_until(
    *,
    last_observed_at: datetime,
    cadence_seconds: float,
) -> datetime:
    ttl = timedelta(seconds=cadence_seconds * 3)
    ttl = max(DEFAULT_HYPOTHESIS_TTL_MIN, ttl)
    ttl = min(DEFAULT_HYPOTHESIS_TTL_MAX, ttl)
    return _utc(last_observed_at) + ttl


def _confidence(
    *,
    occurrence_count: int,
    error_ratio: float,
) -> float:
    stability = max(0.0, min(1.0, 1.0 - error_ratio))
    recurrence_bonus = min(0.15, max(0, occurrence_count - 3) * 0.05)
    return round(min(0.95, 0.60 + recurrence_bonus + 0.20 * stability), 3)


def detect_temporal_recurrence_hypotheses(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    now: datetime | None = None,
    lookback_days: int = 60,
    event_limit: int = 300,
    result_limit: int = 20,
) -> tuple[ContextPatternHypothesis, ...]:
    """Detect bounded recurrence hypotheses from canonical timeline evidence.

    The output is read-only evidence. It never persists a fact, creates a
    recommendation, or grants execution/disclosure authority.
    """

    if not actor_id.strip():
        return ()
    if lookback_days < 1 or lookback_days > 365:
        raise ValueError("PATTERN_LOOKBACK_OUT_OF_RANGE")
    if event_limit < 3 or event_limit > 500:
        raise ValueError("PATTERN_EVENT_LIMIT_OUT_OF_RANGE")
    if result_limit < 1 or result_limit > 50:
        raise ValueError("PATTERN_RESULT_LIMIT_OUT_OF_RANGE")

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
            .order_by(TimelineEventRow.occurred_at.desc(), TimelineEventRow.id.desc())
            .limit(event_limit)
        ).all()
    )

    grouped: dict[tuple[str, str, str], list[TimelineEventRow]] = {}
    for row in rows:
        signature = _event_signature(row)
        if signature is None:
            continue
        signature_kind, signature_value = signature
        key = (row.event_type, signature_kind, signature_value)
        grouped.setdefault(key, []).append(row)

    hypotheses: list[ContextPatternHypothesis] = []
    for (event_type, signature_kind, signature_value), group_rows in grouped.items():
        ordered = sorted(
            group_rows,
            key=lambda item: (_utc(item.occurred_at), item.id),
        )[-MAX_GROUP_EVENTS:]
        recurrence = _best_recurrence_chain(ordered)
        if recurrence is None:
            continue
        chain, cadence_seconds, error_ratio = recurrence
        first_observed_at = _utc(chain[0].occurred_at)
        last_observed_at = _utc(chain[-1].occurred_at)
        valid_until = _hypothesis_valid_until(
            last_observed_at=last_observed_at,
            cadence_seconds=cadence_seconds,
        )
        if valid_until <= stamp:
            continue

        chain_ids = {row.id for row in chain}
        provenance = tuple(sorted({row.provenance for row in chain}))
        hypothesis_id = "pattern-" + stable_hash(
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pattern_type": "TEMPORAL_RECURRENCE",
                "event_type": event_type,
                "signature_kind": signature_kind,
                "signature_value": signature_value,
            }
        )[:32]
        hypotheses.append(
            ContextPatternHypothesis(
                hypothesis_id=hypothesis_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                pattern_type="TEMPORAL_RECURRENCE",
                event_type=event_type,
                signature_kind=signature_kind,
                signature_value=signature_value,
                evidence_class="INFERRED",
                status="HYPOTHESIS",
                confidence=_confidence(
                    occurrence_count=len(chain),
                    error_ratio=error_ratio,
                ),
                cadence_seconds=int(round(cadence_seconds)),
                occurrence_count=len(chain),
                anomaly_count=sum(row.id not in chain_ids for row in ordered),
                support_ratio=round(len(chain) / len(ordered), 3),
                first_observed_at=first_observed_at,
                last_observed_at=last_observed_at,
                valid_until=valid_until,
                evidence_timeline_event_ids=tuple(row.id for row in chain),
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
    "ContextPatternHypothesis",
    "detect_temporal_recurrence_hypotheses",
]
