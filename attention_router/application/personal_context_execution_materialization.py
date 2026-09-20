from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_execution_authority import (
    EXECUTION_SCOPE_VERSION,
    evaluate_accepted_recommendation_authority,
)
from attention_router.application.platform.capability_pack import (
    InternalSchedulerProvider,
)
from attention_router.application.platform.execution import execute_capability
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.providers import ProviderRuntimeRegistry
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    ProviderInstanceRow,
    ReminderRow,
)
from attention_router.infrastructure.repository import audit


REMINDER_CAPABILITY: Final = "reminder.create"
REMINDER_PROVIDER_CANONICAL_NAME: Final = "internal:scheduler"
REMINDER_PROVIDER_INTERFACE: Final = "InternalSchedulerProvider"

_SCOPE_KEYS: Final = frozenset(
    {
        "schema_version",
        "tenant_id",
        "actor_id",
        "recommendation_id",
        "recommendation_claim_id",
        "acceptance_event_id",
        "capability",
        "capability_version_id",
        "parameters",
        "policy",
        "grant_ids",
        "authority",
    }
)


class RecommendationMaterializationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecommendationMaterializationOutcome:
    tenant_id: str
    actor_id: str
    recommendation_id: str
    execution_intent_id: str
    status: str
    reason_code: str
    reminder_id: str | None
    authority_assessment_status: str | None
    materialized_at: datetime | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_semantic_scope(
    intent: ExecutionIntentRow,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
) -> None:
    scope = intent.scope or {}
    if set(scope) != _SCOPE_KEYS:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_SCOPE_SCHEMA_MISMATCH"
        )
    if stable_hash(scope) != intent.scope_fingerprint:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_SCOPE_FINGERPRINT_MISMATCH"
        )
    if scope.get("schema_version") != EXECUTION_SCOPE_VERSION:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_SCOPE_VERSION_MISMATCH"
        )
    if scope.get("tenant_id") != tenant_id:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_TENANT_MISMATCH"
        )
    if scope.get("actor_id") != actor_key:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_ACTOR_MISMATCH"
        )
    if scope.get("recommendation_id") != recommendation_id:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_RECOMMENDATION_MISMATCH"
        )
    if scope.get("capability") != REMINDER_CAPABILITY:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_CAPABILITY_MISMATCH"
        )

    params = scope.get("parameters")
    if not isinstance(params, dict) or set(params) != {"summary", "trigger_at"}:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_PARAMETERS_INVALID"
        )
    if not isinstance(params.get("summary"), str) or not params["summary"].strip():
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_PARAMETERS_INVALID"
        )
    if not isinstance(params.get("trigger_at"), str):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_PARAMETERS_INVALID"
        )

    policy = scope.get("policy")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"policy_id", "policy_version_id"}
        or not isinstance(policy.get("policy_id"), str)
        or not isinstance(policy.get("policy_version_id"), str)
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_POLICY_SCOPE_INVALID"
        )
    grant_ids = scope.get("grant_ids")
    if (
        not isinstance(grant_ids, list)
        or not grant_ids
        or not all(isinstance(item, str) and item for item in grant_ids)
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_GRANT_SCOPE_INVALID"
        )
    authority = scope.get("authority")
    if (
        not isinstance(authority, dict)
        or set(authority)
        != {
            "capability_status",
            "authority_result",
            "provider_instance_id",
            "approval_required",
        }
        or authority.get("authority_result") != "ALLOW"
        or authority.get("approval_required") is not False
        or not isinstance(authority.get("provider_instance_id"), str)
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_AUTHORITY_SCOPE_INVALID"
        )
    if not isinstance(scope.get("capability_version_id"), str):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_CAPABILITY_VERSION_INVALID"
        )
    if not isinstance(scope.get("acceptance_event_id"), str):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_ACCEPTANCE_EVENT_INVALID"
        )

    provenance = intent.provenance or {}
    if (
        provenance.get("origin") != "PERSONAL_CONTEXT_RECOMMENDATION"
        or provenance.get("recommendation_claim_id")
        != scope.get("recommendation_claim_id")
        or provenance.get("acceptance_event_id")
        != scope.get("acceptance_event_id")
        or provenance.get("policy_id") != policy.get("policy_id")
        or provenance.get("policy_version_id")
        != policy.get("policy_version_id")
        or provenance.get("grant_ids") != grant_ids
        or provenance.get("capability_version_id")
        != scope.get("capability_version_id")
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_PROVENANCE_MISMATCH"
        )


def _retire_intent(
    session: Session,
    intent: ExecutionIntentRow,
    *,
    timestamp: datetime,
    reason_code: str,
    tenant_id: str,
    recommendation_id: str,
) -> None:
    if intent.state in {"MATERIALIZED", "RETIRED"}:
        return
    if intent.state not in {"PREPARED", "FROZEN"}:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_INTENT_STATE_INVALID"
        )
    intent.state = "RETIRED"
    intent.retired_at = timestamp
    audit(
        session,
        None,
        "personal_context.recommendation_execution_retired",
        {
            "recommendation_id": recommendation_id,
            "execution_intent_id": intent.id,
            "reason_code": reason_code,
        },
        origin="personal_context",
        tenant_id=tenant_id,
        created_at=timestamp,
    )
    session.flush()


def _existing_materialized_reminder(
    session: Session,
    *,
    intent: ExecutionIntentRow,
    tenant_id: str,
    actor_key: str,
) -> ReminderRow:
    row = session.scalar(
        select(ReminderRow).where(
            ReminderRow.tenant_id == tenant_id,
            ReminderRow.idempotency_key == intent.idempotency_key,
        )
    )
    if (
        row is None
        or row.owner_actor_id != actor_key
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_MATERIALIZED_REMINDER_MISSING"
        )
    return row


def _runtime_registry(
    session: Session,
    *,
    tenant_id: str,
    provider_instance_id: str,
) -> ProviderRuntimeRegistry:
    instance = session.get(ProviderInstanceRow, provider_instance_id)
    if (
        instance is None
        or instance.tenant_id != tenant_id
        or instance.canonical_name != REMINDER_PROVIDER_CANONICAL_NAME
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_PROVIDER_RUNTIME_UNSUPPORTED"
        )
    registry = ProviderRuntimeRegistry()
    registry.register(
        instance.id,
        InternalSchedulerProvider(
            session,
            tenant_id,
            REMINDER_PROVIDER_INTERFACE,
        ),
    )
    return registry


def _request_from_intent(
    intent: ExecutionIntentRow,
    *,
    actor_key: str,
) -> CapabilityRequest:
    scope = intent.scope
    parameters = dict(scope["parameters"])
    parameters["_execution"] = {
        "actor_id": actor_key,
        "correlation_id": intent.idempotency_key,
        "causation_id": scope["acceptance_event_id"],
        "idempotency_key": intent.idempotency_key,
        "capability": REMINDER_CAPABILITY,
        "provider": REMINDER_PROVIDER_CANONICAL_NAME,
        "execution_intent_id": intent.id,
    }
    return CapabilityRequest(
        capability=REMINDER_CAPABILITY,
        parameters=parameters,
        user_requested=True,
        confidence="high",
    )


def materialize_prepared_recommendation_execution(
    session: Session,
    *,
    execution_intent_id: str,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
    now: datetime | None = None,
) -> RecommendationMaterializationOutcome:
    """Materialize one reminder only after fresh authority revalidation.

    PREPARED/FROZEN intents are inert. The capability resolver is evaluated once
    through V1E and again inside execute_capability before provider invocation.
    """

    stamp = _utc(now or datetime.now(UTC))
    intent = session.execute(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    if intent is None:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_INTENT_NOT_FOUND"
        )

    _validate_semantic_scope(
        intent,
        tenant_id=tenant_id,
        actor_key=actor_key,
        recommendation_id=recommendation_id,
    )

    if intent.state == "MATERIALIZED":
        reminder = _existing_materialized_reminder(
            session,
            intent=intent,
            tenant_id=tenant_id,
            actor_key=actor_key,
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status="ALREADY_MATERIALIZED",
            reason_code="IDEMPOTENT_REPLAY",
            reminder_id=reminder.id,
            authority_assessment_status=None,
            materialized_at=_utc(reminder.created_at),
        )
    if intent.state == "RETIRED":
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status="RETIRED",
            reason_code="EXECUTION_INTENT_RETIRED",
            reminder_id=None,
            authority_assessment_status=None,
            materialized_at=None,
        )
    if intent.state not in {"PREPARED", "FROZEN"}:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_EXECUTION_INTENT_STATE_INVALID"
        )
    if intent.expires_at is None or _utc(intent.expires_at) <= stamp:
        _retire_intent(
            session,
            intent,
            timestamp=stamp,
            reason_code="EXECUTION_INTENT_EXPIRED",
            tenant_id=tenant_id,
            recommendation_id=recommendation_id,
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status="EXPIRED",
            reason_code="EXECUTION_INTENT_EXPIRED",
            reminder_id=None,
            authority_assessment_status=None,
            materialized_at=None,
        )

    assessment = evaluate_accepted_recommendation_authority(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        recommendation_id=recommendation_id,
        now=stamp,
    )
    if (
        assessment.assessment_status != "INTENT_PREPARED"
        or assessment.execution_intent_id != intent.id
    ):
        _retire_intent(
            session,
            intent,
            timestamp=stamp,
            reason_code=assessment.reason_code,
            tenant_id=tenant_id,
            recommendation_id=recommendation_id,
        )
        status = (
            "APPROVAL_REQUIRED"
            if assessment.assessment_status == "REQUIRES_APPROVAL"
            else "AUTHORITY_REVALIDATION_BLOCKED"
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status=status,
            reason_code=assessment.reason_code,
            reminder_id=None,
            authority_assessment_status=assessment.assessment_status,
            materialized_at=None,
        )

    current_scope = intent.scope or {}
    if (
        current_scope.get("policy", {}).get("policy_id")
        != assessment.policy_id
        or current_scope.get("policy", {}).get("policy_version_id")
        != assessment.policy_version_id
        or tuple(current_scope.get("grant_ids") or ())
        != assessment.active_grant_ids
        or current_scope.get("authority", {}).get("provider_instance_id")
        != assessment.provider_instance_id
    ):
        _retire_intent(
            session,
            intent,
            timestamp=stamp,
            reason_code="AUTHORITY_SCOPE_CHANGED",
            tenant_id=tenant_id,
            recommendation_id=recommendation_id,
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status="AUTHORITY_REVALIDATION_BLOCKED",
            reason_code="AUTHORITY_SCOPE_CHANGED",
            reminder_id=None,
            authority_assessment_status=assessment.assessment_status,
            materialized_at=None,
        )

    try:
        runtime = _runtime_registry(
            session,
            tenant_id=tenant_id,
            provider_instance_id=assessment.provider_instance_id or "",
        )
    except RecommendationMaterializationError as exc:
        reason_code = str(exc)
        _retire_intent(
            session,
            intent,
            timestamp=stamp,
            reason_code=reason_code,
            tenant_id=tenant_id,
            recommendation_id=recommendation_id,
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status="EXECUTION_BLOCKED",
            reason_code=reason_code,
            reminder_id=None,
            authority_assessment_status=assessment.assessment_status,
            materialized_at=None,
        )

    if intent.state == "PREPARED":
        intent.state = "FROZEN"
        intent.frozen_at = stamp
        session.flush()
    elif intent.frozen_at is None:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_FROZEN_INTENT_TIMESTAMP_MISSING"
        )

    request = _request_from_intent(
        intent,
        actor_key=actor_key,
    )
    execution = execute_capability(
        session,
        request,
        tenant_id=tenant_id,
        grantee_type="ACTOR",
        grantee_id=actor_key,
        policy_allows=assessment.policy_allows,
        runtime_registry=runtime,
        approval_granted=False,
        owner_authorized=False,
    )

    if execution.status != "EXECUTED":
        _retire_intent(
            session,
            intent,
            timestamp=stamp,
            reason_code=execution.reason_code,
            tenant_id=tenant_id,
            recommendation_id=recommendation_id,
        )
        status = (
            "APPROVAL_REQUIRED"
            if execution.status == "REQUIRES_APPROVAL"
            else "EXECUTION_BLOCKED"
        )
        return RecommendationMaterializationOutcome(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            execution_intent_id=intent.id,
            status=status,
            reason_code=execution.reason_code,
            reminder_id=None,
            authority_assessment_status=assessment.assessment_status,
            materialized_at=None,
        )

    result = execution.result or {}
    reminder_id = result.get("reminder_id")
    if not isinstance(reminder_id, str) or not reminder_id:
        raise RecommendationMaterializationError(
            "RECOMMENDATION_REMINDER_RESULT_INVALID"
        )
    reminder = session.get(ReminderRow, reminder_id)
    if (
        reminder is None
        or reminder.tenant_id != tenant_id
        or reminder.owner_actor_id != actor_key
        or reminder.idempotency_key != intent.idempotency_key
    ):
        raise RecommendationMaterializationError(
            "RECOMMENDATION_REMINDER_RESULT_MISMATCH"
        )

    intent.state = "MATERIALIZED"
    audit(
        session,
        None,
        "personal_context.recommendation_materialized",
        {
            "recommendation_id": recommendation_id,
            "execution_intent_id": intent.id,
            "reminder_id": reminder.id,
            "capability": REMINDER_CAPABILITY,
            "policy_id": assessment.policy_id,
            "policy_version_id": assessment.policy_version_id,
            "grant_ids": list(assessment.active_grant_ids),
            "provider_instance_id": assessment.provider_instance_id,
        },
        policy_version_id=assessment.policy_version_id,
        causation_id=(intent.scope or {}).get("acceptance_event_id"),
        origin="personal_context",
        tenant_id=tenant_id,
        created_at=stamp,
    )
    session.flush()

    return RecommendationMaterializationOutcome(
        tenant_id=tenant_id,
        actor_id=actor_key,
        recommendation_id=recommendation_id,
        execution_intent_id=intent.id,
        status="MATERIALIZED",
        reason_code=execution.reason_code,
        reminder_id=reminder.id,
        authority_assessment_status=assessment.assessment_status,
        materialized_at=stamp,
    )


__all__ = [
    "RecommendationMaterializationError",
    "RecommendationMaterializationOutcome",
    "materialize_prepared_recommendation_execution",
]
