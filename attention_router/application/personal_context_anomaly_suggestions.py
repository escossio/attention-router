from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import MemoryActorRow, MemoryClaimRow
from attention_router.application.personal_context_controls import (
    claim_is_owner_non_actionable,
    claim_is_owner_private,
)


ANOMALY_CLAIM_PREDICATE: Final = "context.pattern.sequence_anomaly"
ANOMALY_CLAIM_SOURCE_QUALITY: Final = "DERIVED_PATTERN"
SUGGESTION_CLAIM_PREDICATE: Final = "context.suggestion.proactive"
SUGGESTION_SOURCE_QUALITY: Final = "DERIVED_SUGGESTION"

MIN_ANOMALY_CONFIDENCE: Final = 0.65
MIN_SOURCE_SEQUENCE_CONFIDENCE: Final = 0.80
MIN_SOURCE_SEQUENCE_SUPPORT_RATIO: Final = 0.75
MIN_SOURCE_SEQUENCE_OCCURRENCES: Final = 3
SUGGESTION_TTL: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ContextAnomalySuggestion:
    suggestion_id: str
    tenant_id: str
    actor_id: str
    source_anomaly_claim_id: str
    source_sequence_claim_id: str
    suggestion_type: str
    status: str
    explanation: str
    confidence: float
    generated_at: datetime
    valid_until: datetime
    requires_user_confirmation: bool = True
    execution_requested: bool = False
    grants_authority: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _active_anomaly_claims(
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
            MemoryClaimRow.predicate == ANOMALY_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == ANOMALY_CLAIM_SOURCE_QUALITY,
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


def _suggestion_from_anomaly(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    claim: MemoryClaimRow,
    now: datetime,
) -> ContextAnomalySuggestion | None:
    value = claim.object_json or {}
    context = claim.context or {}
    if value.get("pattern_type") != "SEQUENCE_ANOMALY":
        return None
    if value.get("anomaly_type") != "MISSING_EXPECTED_STEP":
        return None
    if value.get("evidence_class") != "INFERRED":
        return None
    if value.get("hypothesis_status") != "HYPOTHESIS":
        return None
    if value.get("grants_authority") is not False:
        return None
    if value.get("recommendation_ready") is not False:
        return None
    if claim.confidence < MIN_ANOMALY_CONFIDENCE:
        return None

    source_sequence_claim_id = context.get("source_sequence_claim_id")
    if not isinstance(source_sequence_claim_id, str):
        return None
    source = session.get(MemoryClaimRow, source_sequence_claim_id)
    if (
        source is None
        or source.subject_actor_id != claim.subject_actor_id
        or source.status != "ACTIVE"
        or source.predicate != "context.pattern.event_sequence"
        or source.source_quality != "DERIVED_PATTERN"
        or source.sensitivity_class == "SECRET"
    ):
        return None

    if claim_is_owner_private(session, claim=source):
        return None
    if claim_is_owner_non_actionable(session, claim=source):
        return None

    source_value = source.object_json or {}
    source_context = source.context or {}
    support_ratio = source_context.get("support_ratio")
    occurrence_count = source_context.get("occurrence_count")
    if (
        source_value.get("pattern_type") != "EVENT_SEQUENCE"
        or source_value.get("evidence_class") != "INFERRED"
        or source_value.get("hypothesis_status") != "HYPOTHESIS"
        or source_value.get("grants_authority") is not False
        or source.confidence < MIN_SOURCE_SEQUENCE_CONFIDENCE
        or not isinstance(support_ratio, (int, float))
        or float(support_ratio) < MIN_SOURCE_SEQUENCE_SUPPORT_RATIO
        or not isinstance(occurrence_count, int)
        or occurrence_count < MIN_SOURCE_SEQUENCE_OCCURRENCES
        or (
            source.valid_until is not None
            and _utc(source.valid_until) <= now
        )
    ):
        return None

    expected_event_type = value.get("expected_second_event_type")
    expected_kind = value.get("expected_second_signature_kind")
    expected_value = value.get("expected_second_signature_value")
    if not all(
        isinstance(item, str) and item
        for item in (expected_event_type, expected_kind, expected_value)
    ):
        return None

    anomaly_id = context.get("anomaly_id")
    if not isinstance(anomaly_id, str) or not anomaly_id:
        return None

    valid_until = min(
        _utc(claim.valid_until) if claim.valid_until is not None else now + SUGGESTION_TTL,
        now + SUGGESTION_TTL,
    )
    if valid_until <= now:
        return None

    explanation = (
        "Percebi que uma etapa que costuma acontecer depois desta rotina "
        "não apareceu dentro da janela esperada. Quer que eu te mostre o "
        "contexto que mudou?"
    )
    suggestion_id = "suggestion-" + stable_hash(
        {
            "tenant_id": tenant_id,
            "actor_id": actor_key,
            "source_anomaly_claim_id": claim.id,
            "source_sequence_claim_id": source.id,
            "suggestion_type": "REVIEW_MISSING_ROUTINE_STEP",
        }
    )[:32]

    return ContextAnomalySuggestion(
        suggestion_id=suggestion_id,
        tenant_id=tenant_id,
        actor_id=actor_key,
        source_anomaly_claim_id=claim.id,
        source_sequence_claim_id=source.id,
        suggestion_type="REVIEW_MISSING_ROUTINE_STEP",
        status="PROPOSED",
        explanation=explanation,
        confidence=claim.confidence,
        generated_at=now,
        valid_until=valid_until,
        requires_user_confirmation=True,
        execution_requested=False,
        grants_authority=False,
    )


def build_anomaly_suggestions(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime | None = None,
    limit: int = 4,
) -> tuple[ContextAnomalySuggestion, ...]:
    """Build bounded review suggestions from active missing-step anomalies."""

    if limit < 1 or limit > 20:
        raise ValueError("ANOMALY_SUGGESTION_LIMIT_OUT_OF_RANGE")
    if not actor_key.strip():
        return ()

    stamp = _utc(now or datetime.now(UTC))
    suggestions: list[ContextAnomalySuggestion] = []
    for claim in _active_anomaly_claims(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        now=stamp,
    ):
        item = _suggestion_from_anomaly(
            session,
            tenant_id=tenant_id,
            actor_key=actor_key,
            claim=claim,
            now=stamp,
        )
        if item is not None:
            suggestions.append(item)
        if len(suggestions) >= limit:
            break
    return tuple(suggestions)


__all__ = [
    "ContextAnomalySuggestion",
    "MIN_ANOMALY_CONFIDENCE",
    "SUGGESTION_CLAIM_PREDICATE",
    "SUGGESTION_SOURCE_QUALITY",
    "build_anomaly_suggestions",
]
