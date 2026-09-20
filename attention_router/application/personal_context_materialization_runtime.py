from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_execution_materialization import (
    RecommendationMaterializationError,
    materialize_prepared_recommendation_execution,
)
from attention_router.application.personal_context_execution_authority import (
    EXECUTION_SCOPE_VERSION,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ExecutionIntentRow
from attention_router.infrastructure.repository import audit


@dataclass(frozen=True, slots=True)
class PersonalContextMaterializationRuntimeResult:
    intents_scanned: int = 0
    materialized: int = 0
    already_materialized: int = 0
    retired: int = 0
    expired: int = 0
    approval_required: int = 0
    authority_blocked: int = 0
    execution_blocked: int = 0
    malformed: int = 0
    failed: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _candidate_intents(
    session: Session,
    *,
    limit: int,
) -> tuple[ExecutionIntentRow, ...]:
    rows = session.scalars(
        select(ExecutionIntentRow)
        .where(
            ExecutionIntentRow.state.in_(("PREPARED", "FROZEN")),
            ExecutionIntentRow.idempotency_key.like("personal-context:%"),
        )
        .order_by(ExecutionIntentRow.created_at, ExecutionIntentRow.id)
        .limit(limit)
    ).all()
    return tuple(
        row
        for row in rows
        if (row.provenance or {}).get("origin")
        == "PERSONAL_CONTEXT_RECOMMENDATION"
    )


def _scope_identity(
    intent: ExecutionIntentRow,
) -> tuple[str, str, str] | None:
    scope = intent.scope or {}
    if scope.get("schema_version") != EXECUTION_SCOPE_VERSION:
        return None
    tenant_id = scope.get("tenant_id")
    actor_id = scope.get("actor_id")
    recommendation_id = scope.get("recommendation_id")
    if not all(
        isinstance(item, str) and item
        for item in (tenant_id, actor_id, recommendation_id)
    ):
        return None
    return tenant_id, actor_id, recommendation_id


def run_personal_context_materialization_cycle(
    session: Session,
    *,
    now: datetime | None = None,
    intent_limit: int = 50,
) -> PersonalContextMaterializationRuntimeResult:
    """Materialize governed Personal Context intents through V1F only.

    The cycle may create ReminderRow via the internal scheduler provider after
    V1F completes both authority revalidations. It performs no external
    delivery and introduces no alternate execution path.
    """

    if intent_limit < 1 or intent_limit > 500:
        raise ValueError(
            "PERSONAL_CONTEXT_MATERIALIZATION_INTENT_LIMIT_OUT_OF_RANGE"
        )

    stamp = _utc(now or datetime.now(UTC))
    candidates = _candidate_intents(session, limit=intent_limit)
    counters = {
        "intents_scanned": len(candidates),
        "materialized": 0,
        "already_materialized": 0,
        "retired": 0,
        "expired": 0,
        "approval_required": 0,
        "authority_blocked": 0,
        "execution_blocked": 0,
        "malformed": 0,
        "failed": 0,
    }

    for intent in candidates:
        identity = _scope_identity(intent)
        if identity is None:
            counters["malformed"] += 1
            audit(
                session,
                None,
                "personal_context.materialization_runtime_intent_rejected",
                {
                    "execution_intent_id": intent.id,
                    "reason_code": "EXECUTION_INTENT_SCOPE_UNUSABLE",
                    "scope_fingerprint_hash": stable_hash(
                        intent.scope_fingerprint
                    )[:16],
                },
                origin="personal_context",
                tenant_id=(intent.scope or {}).get("tenant_id"),
                created_at=stamp,
            )
            continue

        tenant_id, actor_key, recommendation_id = identity
        try:
            with session.begin_nested():
                outcome = materialize_prepared_recommendation_execution(
                    session,
                    execution_intent_id=intent.id,
                    tenant_id=tenant_id,
                    actor_key=actor_key,
                    recommendation_id=recommendation_id,
                    now=stamp,
                )
        except RecommendationMaterializationError as exc:
            counters["failed"] += 1
            audit(
                session,
                None,
                "personal_context.materialization_runtime_failed",
                {
                    "execution_intent_id": intent.id,
                    "recommendation_id": recommendation_id,
                    "actor_key_hash": stable_hash(actor_key)[:16],
                    "reason_code": str(exc),
                },
                origin="personal_context",
                tenant_id=tenant_id,
                created_at=stamp,
            )
            continue
        except Exception as exc:
            counters["failed"] += 1
            audit(
                session,
                None,
                "personal_context.materialization_runtime_failed",
                {
                    "execution_intent_id": intent.id,
                    "recommendation_id": recommendation_id,
                    "actor_key_hash": stable_hash(actor_key)[:16],
                    "error_class": type(exc).__name__,
                },
                origin="personal_context",
                tenant_id=tenant_id,
                created_at=stamp,
            )
            continue

        if outcome.status == "MATERIALIZED":
            counters["materialized"] += 1
        elif outcome.status == "ALREADY_MATERIALIZED":
            counters["already_materialized"] += 1
        elif outcome.status == "RETIRED":
            counters["retired"] += 1
        elif outcome.status == "EXPIRED":
            counters["expired"] += 1
        elif outcome.status == "APPROVAL_REQUIRED":
            counters["approval_required"] += 1
        elif outcome.status == "AUTHORITY_REVALIDATION_BLOCKED":
            counters["authority_blocked"] += 1
        elif outcome.status == "EXECUTION_BLOCKED":
            counters["execution_blocked"] += 1
        else:
            counters["failed"] += 1
            audit(
                session,
                None,
                "personal_context.materialization_runtime_failed",
                {
                    "execution_intent_id": intent.id,
                    "recommendation_id": recommendation_id,
                    "reason_code": "MATERIALIZATION_OUTCOME_UNSUPPORTED",
                    "outcome_status": outcome.status,
                },
                origin="personal_context",
                tenant_id=tenant_id,
                created_at=stamp,
            )

    return PersonalContextMaterializationRuntimeResult(**counters)


__all__ = [
    "PersonalContextMaterializationRuntimeResult",
    "run_personal_context_materialization_cycle",
]
