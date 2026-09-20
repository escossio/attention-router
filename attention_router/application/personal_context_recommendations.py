from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.platform.registry import capability_and_version
from attention_router.core.capabilities import CapabilityAvailability
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import MemoryActorRow, MemoryClaimRow


PATTERN_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
PATTERN_CLAIM_PREDICATE: Final = "context.pattern.temporal_recurrence"
REMINDER_CAPABILITY: Final = "reminder.create"

MIN_RECOMMENDATION_CONFIDENCE: Final = 0.80
MIN_RECOMMENDATION_SUPPORT_RATIO: Final = 0.75

_USABLE_CAPABILITY_STATES: Final = frozenset(
    {
        CapabilityAvailability.PROVISIONED.value,
        CapabilityAvailability.SANDBOX_PROVED.value,
        CapabilityAvailability.OPERATIONAL.value,
    }
)


@dataclass(frozen=True, slots=True)
class ContextRecommendation:
    recommendation_id: str
    tenant_id: str
    actor_id: str
    source_claim_id: str
    recommendation_type: str
    status: str
    explanation: str
    capability_name: str
    capability_availability: str
    suggested_parameters: dict[str, Any]
    confidence: float
    support_ratio: float
    occurrence_count: int
    anomaly_count: int
    source_provenance: tuple[str, ...]
    generated_at: datetime
    valid_until: datetime
    requires_user_confirmation: bool = True
    execution_requested: bool = False
    grants_authority: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _next_occurrence(
    *,
    last_observed_at: datetime,
    cadence_seconds: int,
    now: datetime,
) -> datetime:
    if cadence_seconds <= 0:
        raise ValueError("RECOMMENDATION_CADENCE_INVALID")

    last = _utc(last_observed_at)
    stamp = _utc(now)
    cadence = timedelta(seconds=cadence_seconds)

    if last > stamp:
        return last

    elapsed = (stamp - last).total_seconds()
    steps = int(elapsed // cadence_seconds) + 1
    return last + cadence * steps


def _reminder_capability_is_usable(
    session: Session,
    *,
    tenant_id: str,
) -> tuple[bool, str]:
    definition, version = capability_and_version(
        session,
        tenant_id,
        REMINDER_CAPABILITY,
    )
    if definition is None or version is None:
        return False, "UNREGISTERED"
    if definition.availability_state not in _USABLE_CAPABILITY_STATES:
        return False, definition.availability_state

    required = set((version.input_schema or {}).get("required") or [])
    if not {"summary", "trigger_at"}.issubset(required):
        return False, "CONTRACT_MISMATCH"

    return True, definition.availability_state


def _active_pattern_claims(
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
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == PATTERN_CLAIM_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
            MemoryClaimRow.sensitivity_class != "SECRET",
            or_(MemoryClaimRow.valid_from.is_(None), MemoryClaimRow.valid_from <= now),
            or_(MemoryClaimRow.valid_until.is_(None), MemoryClaimRow.valid_until > now),
        )
        .order_by(
            MemoryClaimRow.confidence.desc(),
            MemoryClaimRow.last_observed_at.desc(),
            MemoryClaimRow.id,
        )
    ).all()
    return tuple(rows)


def _recommendation_from_claim(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    claim: MemoryClaimRow,
    now: datetime,
) -> ContextRecommendation | None:
    value = claim.object_json or {}
    context = claim.context or {}

    if value.get("pattern_type") != "TEMPORAL_RECURRENCE":
        return None
    if value.get("event_type") != "LOCATION_ARRIVAL":
        return None
    if value.get("evidence_class") != "INFERRED":
        return None
    if value.get("hypothesis_status") != "HYPOTHESIS":
        return None
    if value.get("grants_authority") is not False:
        return None
    if value.get("recommendation_ready") is not False:
        return None

    support_ratio = context.get("support_ratio")
    occurrence_count = context.get("occurrence_count")
    anomaly_count = context.get("anomaly_count")
    source_provenance = context.get("source_provenance")
    cadence_seconds = value.get("cadence_seconds")

    if not isinstance(support_ratio, (int, float)):
        return None
    if not isinstance(occurrence_count, int):
        return None
    if not isinstance(anomaly_count, int):
        return None
    if not isinstance(cadence_seconds, int):
        return None
    if not isinstance(source_provenance, list) or not all(
        isinstance(item, str) for item in source_provenance
    ):
        return None

    if claim.confidence < MIN_RECOMMENDATION_CONFIDENCE:
        return None
    if float(support_ratio) < MIN_RECOMMENDATION_SUPPORT_RATIO:
        return None
    if occurrence_count < 3:
        return None

    usable, availability = _reminder_capability_is_usable(
        session,
        tenant_id=tenant_id,
    )
    if not usable:
        return None

    next_occurrence = _next_occurrence(
        last_observed_at=claim.last_observed_at,
        cadence_seconds=cadence_seconds,
        now=now,
    )
    claim_valid_until = (
        _utc(claim.valid_until)
        if claim.valid_until is not None
        else next_occurrence
    )
    valid_until = min(claim_valid_until, next_occurrence)
    if valid_until <= now:
        return None

    cadence_hours = cadence_seconds / 3600
    cadence_text = (
        f"{cadence_hours:.0f} horas"
        if cadence_hours >= 1
        else f"{cadence_seconds // 60} minutos"
    )
    explanation = (
        "Percebi uma recorrência de chegada com "
        f"{occurrence_count} ocorrências e cadência aproximada de "
        f"{cadence_text}. Quer que eu prepare um lembrete para a "
        "próxima ocorrência prevista?"
    )

    recommendation_id = "recommendation-" + stable_hash(
        {
            "tenant_id": tenant_id,
            "actor_id": actor_key,
            "source_claim_id": claim.id,
            "recommendation_type": "REMINDER_FOR_RECURRENT_ARRIVAL",
            "capability_name": REMINDER_CAPABILITY,
            "trigger_at": next_occurrence.isoformat(),
        }
    )[:32]

    return ContextRecommendation(
        recommendation_id=recommendation_id,
        tenant_id=tenant_id,
        actor_id=actor_key,
        source_claim_id=claim.id,
        recommendation_type="REMINDER_FOR_RECURRENT_ARRIVAL",
        status="PROPOSED",
        explanation=explanation,
        capability_name=REMINDER_CAPABILITY,
        capability_availability=availability,
        suggested_parameters={
            "summary": "Recorrência observada pela Andy",
            "trigger_at": next_occurrence.isoformat(),
        },
        confidence=claim.confidence,
        support_ratio=round(float(support_ratio), 3),
        occurrence_count=occurrence_count,
        anomaly_count=anomaly_count,
        source_provenance=tuple(sorted(set(source_provenance))),
        generated_at=now,
        valid_until=valid_until,
        requires_user_confirmation=True,
        execution_requested=False,
        grants_authority=False,
    )


def build_context_recommendations(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
    limit: int = 8,
) -> tuple[ContextRecommendation, ...]:
    """Build bounded proactive opportunities from active Personal Context.

    Recommendations are proposal-only knowledge. This function never invokes a
    capability resolver, executor, scheduler, outbox, or delivery path.
    """

    if limit < 1 or limit > 20:
        raise ValueError("CONTEXT_RECOMMENDATION_LIMIT_OUT_OF_RANGE")
    if not actor_key.strip():
        return ()

    stamp = _utc(now or datetime.now(UTC))
    rows = _active_pattern_claims(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        now=stamp,
    )

    recommendations: list[ContextRecommendation] = []
    for row in rows:
        item = _recommendation_from_claim(
            session,
            tenant_id=tenant_id,
            actor_key=actor_key,
            claim=row,
            now=stamp,
        )
        if item is not None:
            recommendations.append(item)
        if len(recommendations) >= limit:
            break

    return tuple(recommendations)


__all__ = [
    "ContextRecommendation",
    "MIN_RECOMMENDATION_CONFIDENCE",
    "MIN_RECOMMENDATION_SUPPORT_RATIO",
    "REMINDER_CAPABILITY",
    "build_context_recommendations",
]
