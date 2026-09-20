from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_execution_authority import (
    RecommendationAuthorityError,
    evaluate_accepted_recommendation_authority,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RECOMMENDATION_SOURCE_QUALITY,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    MemoryActorRow,
    MemoryClaimRow,
)
from attention_router.infrastructure.repository import audit


@dataclass(frozen=True, slots=True)
class PersonalContextAuthorityRuntimeResult:
    owners_scanned: int = 0
    recommendations_scanned: int = 0
    assessments_completed: int = 0
    assessments_failed: int = 0
    intents_prepared: int = 0
    denied: int = 0
    approval_required: int = 0
    capability_unavailable: int = 0
    policy_unresolved: int = 0
    source_invalidated: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _owner_actor_scopes(
    session: Session,
) -> tuple[tuple[str, str], ...]:
    rows = session.scalars(
        select(ActorBindingRow)
        .where(
            ActorBindingRow.is_active.is_(True),
            ActorBindingRow.actor_category == "owner",
        )
        .order_by(
            ActorBindingRow.tenant_id,
            ActorBindingRow.actor_key,
            ActorBindingRow.id,
        )
    ).all()
    scopes = {
        (row.tenant_id, row.actor_key)
        for row in rows
        if (row.binding_metadata or {}).get("owner") is True
    }
    return tuple(sorted(scopes))


def _accepted_recommendations(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    now: datetime,
    limit: int,
) -> tuple[MemoryClaimRow, ...]:
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if actor is None:
        return ()

    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at, MemoryClaimRow.id)
    ).all()
    accepted = [
        row
        for row in rows
        if (row.object_json or {}).get("lifecycle_state") == "ACCEPTED"
        and row.valid_until is not None
        and _utc(row.valid_until) > now
    ]
    return tuple(accepted[:limit])


def run_personal_context_authority_cycle(
    session: Session,
    *,
    now: datetime | None = None,
    owner_limit: int = 50,
    recommendation_limit_per_owner: int = 20,
) -> PersonalContextAuthorityRuntimeResult:
    """Revalidate accepted recommendations without materializing effects.

    This cycle may create an inert PREPARED ExecutionIntentRow through V1E.
    It never calls V1F, never freezes an intent, never invokes a provider and
    never creates a ReminderRow.
    """

    if owner_limit < 1 or owner_limit > 500:
        raise ValueError("PERSONAL_CONTEXT_AUTHORITY_OWNER_LIMIT_OUT_OF_RANGE")
    if recommendation_limit_per_owner < 1 or recommendation_limit_per_owner > 100:
        raise ValueError(
            "PERSONAL_CONTEXT_AUTHORITY_RECOMMENDATION_LIMIT_OUT_OF_RANGE"
        )

    stamp = _utc(now or datetime.now(UTC))
    counters = {
        "owners_scanned": 0,
        "recommendations_scanned": 0,
        "assessments_completed": 0,
        "assessments_failed": 0,
        "intents_prepared": 0,
        "denied": 0,
        "approval_required": 0,
        "capability_unavailable": 0,
        "policy_unresolved": 0,
        "source_invalidated": 0,
    }

    for tenant_id, actor_key in _owner_actor_scopes(session)[:owner_limit]:
        counters["owners_scanned"] += 1
        recommendations = _accepted_recommendations(
            session,
            tenant_id=tenant_id,
            actor_key=actor_key,
            now=stamp,
            limit=recommendation_limit_per_owner,
        )
        counters["recommendations_scanned"] += len(recommendations)

        for recommendation in recommendations:
            recommendation_id = (recommendation.context or {}).get(
                "recommendation_id"
            )
            if not isinstance(recommendation_id, str) or not recommendation_id:
                counters["assessments_failed"] += 1
                audit(
                    session,
                    None,
                    "personal_context.authority_runtime_assessment_failed",
                    {
                        "recommendation_claim_id": recommendation.id,
                        "actor_key_hash": stable_hash(actor_key)[:16],
                        "reason_code": "RECOMMENDATION_ID_MISSING",
                    },
                    origin="personal_context",
                    tenant_id=tenant_id,
                    created_at=stamp,
                )
                continue

            try:
                with session.begin_nested():
                    assessment = evaluate_accepted_recommendation_authority(
                        session,
                        tenant_id=tenant_id,
                        actor_key=actor_key,
                        recommendation_id=recommendation_id,
                        now=stamp,
                    )
            except RecommendationAuthorityError as exc:
                counters["assessments_failed"] += 1
                audit(
                    session,
                    None,
                    "personal_context.authority_runtime_assessment_failed",
                    {
                        "recommendation_id": recommendation_id,
                        "recommendation_claim_id": recommendation.id,
                        "actor_key_hash": stable_hash(actor_key)[:16],
                        "reason_code": str(exc),
                    },
                    origin="personal_context",
                    tenant_id=tenant_id,
                    created_at=stamp,
                )
                continue
            except Exception as exc:
                counters["assessments_failed"] += 1
                audit(
                    session,
                    None,
                    "personal_context.authority_runtime_assessment_failed",
                    {
                        "recommendation_id": recommendation_id,
                        "recommendation_claim_id": recommendation.id,
                        "actor_key_hash": stable_hash(actor_key)[:16],
                        "error_class": type(exc).__name__,
                    },
                    origin="personal_context",
                    tenant_id=tenant_id,
                    created_at=stamp,
                )
                continue

            counters["assessments_completed"] += 1
            if assessment.assessment_status == "INTENT_PREPARED":
                counters["intents_prepared"] += 1
            elif assessment.assessment_status == "REQUIRES_APPROVAL":
                counters["approval_required"] += 1
            elif assessment.assessment_status == "CAPABILITY_UNAVAILABLE":
                counters["capability_unavailable"] += 1
            elif assessment.assessment_status == "POLICY_UNRESOLVED":
                counters["policy_unresolved"] += 1
            elif assessment.assessment_status == "SOURCE_INVALIDATED":
                counters["source_invalidated"] += 1
            else:
                counters["denied"] += 1

    return PersonalContextAuthorityRuntimeResult(**counters)


__all__ = [
    "PersonalContextAuthorityRuntimeResult",
    "run_personal_context_authority_cycle",
]
