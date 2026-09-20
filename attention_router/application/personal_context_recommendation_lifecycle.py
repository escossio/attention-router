from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_recommendations import (
    ContextRecommendation,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
)


RECOMMENDATION_CLAIM_PREDICATE: Final = "context.recommendation.proactive"
RECOMMENDATION_SOURCE_QUALITY: Final = "DERIVED_RECOMMENDATION"


class RecommendationLifecycleError(RuntimeError):
    pass


class RecommendationDecision(StrEnum):
    ACCEPT = "ACCEPT"
    DISMISS = "DISMISS"


_TERMINAL_STATES = frozenset({"ACCEPTED", "DISMISSED", "EXPIRED"})


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


def _source_claim_for(
    session: Session,
    *,
    recommendation: ContextRecommendation,
    actor: MemoryActorRow,
) -> MemoryClaimRow:
    claim = session.get(MemoryClaimRow, recommendation.source_claim_id)
    if claim is None:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_CLAIM_NOT_FOUND")
    if claim.subject_actor_id != actor.id:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_ACTOR_MISMATCH")
    if claim.status != "ACTIVE":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_NOT_ACTIVE")
    if claim.source_quality != "DERIVED_PATTERN":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_QUALITY_INVALID")
    value = claim.object_json or {}
    if value.get("evidence_class") != "INFERRED":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_NOT_INFERRED")
    if value.get("hypothesis_status") != "HYPOTHESIS":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_NOT_HYPOTHESIS")
    if value.get("grants_authority") is not False:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_AUTHORITY_INVALID")
    if value.get("pattern_type") != "TEMPORAL_RECURRENCE":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_PATTERN_INVALID")
    if value.get("event_type") != "LOCATION_ARRIVAL":
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_EVENT_INVALID")
    context = claim.context or {}
    if float(context.get("support_ratio", -1.0)) != recommendation.support_ratio:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_SUPPORT_MISMATCH")
    if context.get("occurrence_count") != recommendation.occurrence_count:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_COUNT_MISMATCH")
    if context.get("anomaly_count") != recommendation.anomaly_count:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_ANOMALY_MISMATCH")
    if tuple(sorted(context.get("source_provenance") or [])) != tuple(
        sorted(recommendation.source_provenance)
    ):
        raise RecommendationLifecycleError(
            "RECOMMENDATION_SOURCE_PROVENANCE_MISMATCH"
        )
    if recommendation.confidence != claim.confidence:
        raise RecommendationLifecycleError(
            "RECOMMENDATION_SOURCE_CONFIDENCE_MISMATCH"
        )
    generated_at = _utc(recommendation.generated_at)
    if claim.valid_from is not None and _utc(claim.valid_from) > generated_at:
        raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_NOT_YET_VALID")
    if claim.valid_until is not None:
        source_valid_until = _utc(claim.valid_until)
        if source_valid_until <= generated_at:
            raise RecommendationLifecycleError("RECOMMENDATION_SOURCE_EXPIRED")
        if _utc(recommendation.valid_until) > source_valid_until:
            raise RecommendationLifecycleError(
                "RECOMMENDATION_OUTLIVES_SOURCE"
            )
    return claim


def _active_recommendation_rows(
    session: Session,
    *,
    actor_id: str,
    recommendation_id: str,
) -> list[MemoryClaimRow]:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    return [
        row
        for row in rows
        if (row.context or {}).get("recommendation_id") == recommendation_id
    ]


def _recommendation_fingerprint(
    recommendation: ContextRecommendation,
) -> str:
    return stable_hash(
        {
            "recommendation_id": recommendation.recommendation_id,
            "tenant_id": recommendation.tenant_id,
            "actor_id": recommendation.actor_id,
            "source_claim_id": recommendation.source_claim_id,
            "recommendation_type": recommendation.recommendation_type,
            "capability_name": recommendation.capability_name,
            "capability_availability": recommendation.capability_availability,
            "suggested_parameters": recommendation.suggested_parameters,
            "confidence": recommendation.confidence,
            "support_ratio": recommendation.support_ratio,
            "occurrence_count": recommendation.occurrence_count,
            "anomaly_count": recommendation.anomaly_count,
            "source_provenance": list(recommendation.source_provenance),
            "generated_at": _utc(recommendation.generated_at).isoformat(),
            "valid_until": _utc(recommendation.valid_until).isoformat(),
        }
    )


def _object_json(
    recommendation: ContextRecommendation,
    *,
    lifecycle_state: str,
) -> dict[str, object]:
    return {
        "recommendation_type": recommendation.recommendation_type,
        "lifecycle_state": lifecycle_state,
        "capability_name": recommendation.capability_name,
        "capability_availability": recommendation.capability_availability,
        "suggested_parameters": dict(recommendation.suggested_parameters),
        "requires_user_confirmation": True,
        "execution_requested": False,
        "grants_authority": False,
    }


def _context_json(
    recommendation: ContextRecommendation,
    *,
    fingerprint: str,
    resolution_inbound_event_id: str | None = None,
    resolution_kind: str | None = None,
    resolved_at: datetime | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "recommendation_id": recommendation.recommendation_id,
        "source_claim_id": recommendation.source_claim_id,
        "snapshot_fingerprint": fingerprint,
        "confidence": recommendation.confidence,
        "support_ratio": recommendation.support_ratio,
        "occurrence_count": recommendation.occurrence_count,
        "anomaly_count": recommendation.anomaly_count,
        "source_provenance": list(recommendation.source_provenance),
        "explanation": recommendation.explanation,
    }
    if resolution_inbound_event_id is not None:
        payload["resolution_inbound_event_id"] = resolution_inbound_event_id
    if resolution_kind is not None:
        payload["resolution_kind"] = resolution_kind
    if resolved_at is not None:
        payload["resolved_at"] = _utc(resolved_at).isoformat()
    return payload


def persist_context_recommendation(
    session: Session,
    *,
    recommendation: ContextRecommendation,
) -> tuple[MemoryClaimRow, bool]:
    if recommendation.recommendation_type != "REMINDER_FOR_RECURRENT_ARRIVAL":
        raise RecommendationLifecycleError("RECOMMENDATION_TYPE_UNSUPPORTED")
    if recommendation.capability_name != "reminder.create":
        raise RecommendationLifecycleError("RECOMMENDATION_CAPABILITY_INVALID")
    if recommendation.status != "PROPOSED":
        raise RecommendationLifecycleError("RECOMMENDATION_STATUS_INVALID")
    if recommendation.execution_requested:
        raise RecommendationLifecycleError("RECOMMENDATION_EXECUTION_REQUEST_FORBIDDEN")
    if recommendation.grants_authority:
        raise RecommendationLifecycleError("RECOMMENDATION_AUTHORITY_FORBIDDEN")
    if not recommendation.requires_user_confirmation:
        raise RecommendationLifecycleError("RECOMMENDATION_CONFIRMATION_REQUIRED")
    if _utc(recommendation.valid_until) <= _utc(recommendation.generated_at):
        raise RecommendationLifecycleError("RECOMMENDATION_VALIDITY_INVALID")

    actor = _memory_actor(
        session,
        tenant_id=recommendation.tenant_id,
        actor_key=recommendation.actor_id,
    )
    _source_claim_for(
        session,
        recommendation=recommendation,
        actor=actor,
    )

    active = _active_recommendation_rows(
        session,
        actor_id=actor.id,
        recommendation_id=recommendation.recommendation_id,
    )
    if len(active) > 1:
        raise RecommendationLifecycleError("RECOMMENDATION_ACTIVE_CONFLICT")

    fingerprint = _recommendation_fingerprint(recommendation)
    if active:
        current = active[0]
        if (
            (current.context or {}).get("snapshot_fingerprint") == fingerprint
            and (current.object_json or {}).get("lifecycle_state") == "PROPOSED"
        ):
            return current, False
        raise RecommendationLifecycleError("RECOMMENDATION_ACTIVE_ALREADY_EXISTS")

    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=RECOMMENDATION_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=recommendation.explanation,
        object_actor_id=None,
        object_entity_id=None,
        object_json=_object_json(
            recommendation,
            lifecycle_state="PROPOSED",
        ),
        context=_context_json(
            recommendation,
            fingerprint=fingerprint,
        ),
        confidence=recommendation.confidence,
        sensitivity_class="PRIVATE",
        source_quality=RECOMMENDATION_SOURCE_QUALITY,
        valid_from=_utc(recommendation.generated_at),
        valid_until=_utc(recommendation.valid_until),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=_utc(recommendation.generated_at),
        last_observed_at=_utc(recommendation.generated_at),
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row, True


def _explicit_owner_decision_event(
    session: Session,
    *,
    event: InboundEventRow,
    tenant_id: str,
    actor_key: str,
) -> None:
    if event.tenant_id != tenant_id:
        raise RecommendationLifecycleError("RECOMMENDATION_DECISION_TENANT_MISMATCH")
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
        or metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or metadata.get("from_me_classification") != "OWNER_COMMAND"
        or metadata.get("final_from_me_classification") != "OWNER_COMMAND"
    ):
        raise RecommendationLifecycleError(
            "RECOMMENDATION_DECISION_OWNER_AUTHORITY_UNAVAILABLE"
        )

    external_actor_id = payload.get("actor_id")
    if not isinstance(external_actor_id, str) or not external_actor_id:
        raise RecommendationLifecycleError("RECOMMENDATION_DECISION_ACTOR_MISSING")
    binding = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == event.source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )
    if binding is None:
        raise RecommendationLifecycleError(
            "RECOMMENDATION_DECISION_ACTOR_MISMATCH"
        )


def _event_already_consumed(
    session: Session,
    *,
    actor_id: str,
    event_id: str,
) -> bool:
    rows = session.scalars(
        select(MemoryClaimRow).where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
        )
    ).all()
    return any(
        (row.context or {}).get("resolution_inbound_event_id") == event_id
        for row in rows
    )


def resolve_context_recommendation(
    session: Session,
    *,
    recommendation_id: str,
    tenant_id: str,
    actor_key: str,
    decision: RecommendationDecision | str,
    resolution_event: InboundEventRow,
    now: datetime | None = None,
) -> tuple[MemoryClaimRow, bool]:
    try:
        normalized_decision = (
            decision
            if isinstance(decision, RecommendationDecision)
            else RecommendationDecision(decision)
        )
    except ValueError as exc:
        raise RecommendationLifecycleError(
            "RECOMMENDATION_DECISION_INVALID"
        ) from exc

    stamp = _utc(now or resolution_event.received_at)
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if actor is None:
        raise RecommendationLifecycleError("RECOMMENDATION_ACTOR_NOT_FOUND")

    active = _active_recommendation_rows(
        session,
        actor_id=actor.id,
        recommendation_id=recommendation_id,
    )
    if len(active) != 1:
        raise RecommendationLifecycleError(
            "RECOMMENDATION_ACTIVE_NOT_UNIQUE"
        )
    current = active[0]
    state = (current.object_json or {}).get("lifecycle_state")

    target_state = (
        "ACCEPTED"
        if normalized_decision is RecommendationDecision.ACCEPT
        else "DISMISSED"
    )

    if state in _TERMINAL_STATES:
        if (
            state == target_state
            and (current.context or {}).get("resolution_inbound_event_id")
            == resolution_event.id
        ):
            return current, False
        raise RecommendationLifecycleError(
            "RECOMMENDATION_ALREADY_TERMINAL"
        )
    if state != "PROPOSED":
        raise RecommendationLifecycleError("RECOMMENDATION_STATE_INVALID")
    if current.valid_until is None or _utc(current.valid_until) <= stamp:
        raise RecommendationLifecycleError("RECOMMENDATION_EXPIRED")

    _explicit_owner_decision_event(
        session,
        event=resolution_event,
        tenant_id=tenant_id,
        actor_key=actor_key,
    )
    if _event_already_consumed(
        session,
        actor_id=actor.id,
        event_id=resolution_event.id,
    ):
        raise RecommendationLifecycleError(
            "RECOMMENDATION_DECISION_EVENT_REUSED"
        )

    current.status = "SUPERSEDED"
    current.updated_at = now_utc()

    value = current.object_json or {}
    context = current.context or {}
    row = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=RECOMMENDATION_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=current.object_text,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            **value,
            "lifecycle_state": target_state,
            "execution_requested": False,
            "grants_authority": False,
        },
        context={
            **context,
            "resolution_inbound_event_id": resolution_event.id,
            "resolution_kind": normalized_decision.value,
            "resolved_at": stamp.isoformat(),
        },
        confidence=current.confidence,
        sensitivity_class=current.sensitivity_class,
        source_quality=RECOMMENDATION_SOURCE_QUALITY,
        valid_from=current.valid_from,
        valid_until=current.valid_until,
        status="ACTIVE",
        staleness_class=current.staleness_class,
        supersedes_claim_id=current.id,
        conflict_group_id=None,
        first_observed_at=current.first_observed_at,
        last_observed_at=stamp,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row, True


def expire_due_context_recommendations(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    if limit < 1 or limit > 1000:
        raise ValueError("RECOMMENDATION_EXPIRY_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now or datetime.now(UTC))
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
            MemoryClaimRow.valid_until.is_not(None),
            MemoryClaimRow.valid_until <= stamp,
        )
        .order_by(MemoryClaimRow.valid_until, MemoryClaimRow.id)
        .limit(limit)
    ).all()

    expired = 0
    for current in rows:
        if (current.object_json or {}).get("lifecycle_state") != "PROPOSED":
            continue

        current.status = "SUPERSEDED"
        current.updated_at = now_utc()
        value = current.object_json or {}
        context = current.context or {}
        row = MemoryClaimRow(
            id=new_id(),
            subject_actor_id=current.subject_actor_id,
            subject_entity_id=None,
            predicate=RECOMMENDATION_CLAIM_PREDICATE,
            object_type="JSON",
            object_text=current.object_text,
            object_actor_id=None,
            object_entity_id=None,
            object_json={
                **value,
                "lifecycle_state": "EXPIRED",
                "execution_requested": False,
                "grants_authority": False,
            },
            context={
                **context,
                "resolution_kind": "EXPIRE",
                "resolved_at": stamp.isoformat(),
            },
            confidence=current.confidence,
            sensitivity_class=current.sensitivity_class,
            source_quality=RECOMMENDATION_SOURCE_QUALITY,
            valid_from=current.valid_from,
            valid_until=current.valid_until,
            status="ACTIVE",
            staleness_class=current.staleness_class,
            supersedes_claim_id=current.id,
            conflict_group_id=None,
            first_observed_at=current.first_observed_at,
            last_observed_at=stamp,
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        session.add(row)
        expired += 1

    if expired:
        session.flush()
    return expired


__all__ = [
    "RECOMMENDATION_CLAIM_PREDICATE",
    "RECOMMENDATION_SOURCE_QUALITY",
    "RecommendationDecision",
    "RecommendationLifecycleError",
    "expire_due_context_recommendations",
    "persist_context_recommendation",
    "resolve_context_recommendation",
]
