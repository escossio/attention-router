from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.platform.registry import (
    capability_and_version,
    resolve_capability_request,
)
from attention_router.application.personal_context_recommendation_lifecycle import (
    RECOMMENDATION_CLAIM_PREDICATE,
    RECOMMENDATION_SOURCE_QUALITY,
)
from attention_router.core.capabilities import (
    CapabilityRequest,
    CapabilityResolutionStatus,
)
from attention_router.domain.policies import resolve_policy
from attention_router.domain.models import new_id
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    CapabilityGrantRow,
    ExecutionIntentRow,
    InboundEventRow,
    MemoryActorRow,
    MemoryClaimRow,
)
from attention_router.infrastructure.repository import (
    audit,
    get_active_policy_version,
    list_policies,
)


REMINDER_CAPABILITY: Final = "reminder.create"
EXECUTION_SCOPE_VERSION: Final = "personal-context-recommendation-execution-v1"


class RecommendationAuthorityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecommendationAuthorityAssessment:
    tenant_id: str
    actor_id: str
    recommendation_id: str
    recommendation_claim_id: str
    capability_name: str
    policy_id: str | None
    policy_version_id: str | None
    policy_allows: bool
    active_grant_ids: tuple[str, ...]
    capability_status: str
    authority_result: str
    reason_code: str
    execution_allowed: bool
    approval_required: bool
    provider_instance_id: str | None
    assessment_status: str
    execution_intent_id: str | None
    evaluated_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _accepted_recommendation(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
    now: datetime,
) -> tuple[MemoryActorRow, MemoryClaimRow]:
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    if actor is None:
        raise RecommendationAuthorityError("RECOMMENDATION_ACTOR_NOT_FOUND")

    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == RECOMMENDATION_CLAIM_PREDICATE,
            MemoryClaimRow.source_quality == RECOMMENDATION_SOURCE_QUALITY,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    matching = [
        row
        for row in rows
        if (row.context or {}).get("recommendation_id") == recommendation_id
    ]
    if len(matching) != 1:
        raise RecommendationAuthorityError(
            "RECOMMENDATION_ACCEPTED_SNAPSHOT_NOT_UNIQUE"
        )

    row = matching[0]
    value = row.object_json or {}
    context = row.context or {}
    if value.get("lifecycle_state") != "ACCEPTED":
        raise RecommendationAuthorityError("RECOMMENDATION_NOT_ACCEPTED")
    if value.get("capability_name") != REMINDER_CAPABILITY:
        raise RecommendationAuthorityError("RECOMMENDATION_CAPABILITY_INVALID")
    if value.get("execution_requested") is not False:
        raise RecommendationAuthorityError(
            "RECOMMENDATION_EXECUTION_FLAG_INVALID"
        )
    if value.get("grants_authority") is not False:
        raise RecommendationAuthorityError(
            "RECOMMENDATION_AUTHORITY_FLAG_INVALID"
        )
    if row.valid_until is None or _utc(row.valid_until) <= now:
        raise RecommendationAuthorityError("RECOMMENDATION_EXPIRED")
    if not isinstance(context.get("resolution_inbound_event_id"), str):
        raise RecommendationAuthorityError(
            "RECOMMENDATION_ACCEPTANCE_PROVENANCE_MISSING"
        )
    params = value.get("suggested_parameters")
    if not isinstance(params, dict):
        raise RecommendationAuthorityError(
            "RECOMMENDATION_PARAMETERS_INVALID"
        )
    if set(params) != {"summary", "trigger_at"}:
        raise RecommendationAuthorityError(
            "RECOMMENDATION_PARAMETERS_INVALID"
        )
    if not isinstance(params.get("summary"), str) or not params["summary"].strip():
        raise RecommendationAuthorityError(
            "RECOMMENDATION_PARAMETERS_INVALID"
        )
    if not isinstance(params.get("trigger_at"), str):
        raise RecommendationAuthorityError(
            "RECOMMENDATION_PARAMETERS_INVALID"
        )
    return actor, row


def _acceptance_binding(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_claim: MemoryClaimRow,
) -> ActorBindingRow | None:
    event_id = (recommendation_claim.context or {}).get(
        "resolution_inbound_event_id"
    )
    if not isinstance(event_id, str):
        return None
    event = session.get(InboundEventRow, event_id)
    if event is None or event.tenant_id != tenant_id:
        return None
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    external_actor_id = payload.get("actor_id")
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
        or metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or not isinstance(external_actor_id, str)
    ):
        return None
    return session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.source == event.source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.is_active.is_(True),
        )
    )


def _resolved_policy(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_claim: MemoryClaimRow,
) -> tuple[str | None, str | None, bool, str]:
    binding = _acceptance_binding(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        recommendation_claim=recommendation_claim,
    )
    if binding is None:
        return None, None, False, "ACCEPTANCE_BINDING_UNRESOLVED"

    audience = (binding.binding_metadata or {}).get("audience")
    audience = audience if isinstance(audience, str) else None
    try:
        resolution = resolve_policy(
            list_policies(session, tenant_id),
            actor_key,
            binding.actor_category,
            binding.active_context,
            audience=audience,
            binding_id=binding.id,
        )
        version = get_active_policy_version(
            session,
            resolution.winner.identifier,
            tenant_id,
        )
    except (KeyError, ValueError):
        return None, None, False, "POLICY_UNRESOLVED"

    allowed = (
        REMINDER_CAPABILITY in resolution.winner.allowed_actions
        or "*" in resolution.winner.allowed_actions
    )
    return (
        resolution.winner.identifier,
        version.id,
        allowed,
        "POLICY_ALLOWED" if allowed else "POLICY_DENIED",
    )


def _active_grants(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    capability_id: str,
    now: datetime,
) -> tuple[str, ...]:
    rows = session.scalars(
        select(CapabilityGrantRow)
        .where(
            CapabilityGrantRow.tenant_id == tenant_id,
            CapabilityGrantRow.grantee_type == "ACTOR",
            CapabilityGrantRow.grantee_id == actor_key,
            CapabilityGrantRow.capability_id == capability_id,
            CapabilityGrantRow.status == "ACTIVE",
            CapabilityGrantRow.valid_from <= now,
            or_(
                CapabilityGrantRow.valid_until.is_(None),
                CapabilityGrantRow.valid_until > now,
            ),
            CapabilityGrantRow.target_resource_id.is_(None),
        )
        .order_by(CapabilityGrantRow.created_at, CapabilityGrantRow.id)
    ).all()
    return tuple(row.id for row in rows)


def _intent_scope(
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
    recommendation_claim: MemoryClaimRow,
    policy_id: str,
    policy_version_id: str,
    grant_ids: tuple[str, ...],
    capability_status: str,
    authority_result: str,
    provider_instance_id: str | None,
    capability_version_id: str,
    approval_required: bool,
) -> dict[str, object]:
    value = recommendation_claim.object_json or {}
    context = recommendation_claim.context or {}
    return {
        "schema_version": EXECUTION_SCOPE_VERSION,
        "tenant_id": tenant_id,
        "actor_id": actor_key,
        "recommendation_id": recommendation_id,
        "recommendation_claim_id": recommendation_claim.id,
        "acceptance_event_id": context["resolution_inbound_event_id"],
        "capability": REMINDER_CAPABILITY,
        "capability_version_id": capability_version_id,
        "parameters": dict(value["suggested_parameters"]),
        "policy": {
            "policy_id": policy_id,
            "policy_version_id": policy_version_id,
        },
        "grant_ids": list(grant_ids),
        "authority": {
            "capability_status": capability_status,
            "authority_result": authority_result,
            "provider_instance_id": provider_instance_id,
            "approval_required": approval_required,
        },
    }


def _intent_idempotency_key(
    *,
    scope: dict[str, object],
) -> str:
    return "personal-context:" + stable_hash(scope)[:48]


def _audit_assessment(
    session: Session,
    assessment: RecommendationAuthorityAssessment,
) -> None:
    audit(
        session,
        None,
        "personal_context.recommendation_authority_evaluated",
        {
            "recommendation_id": assessment.recommendation_id,
            "recommendation_claim_id": assessment.recommendation_claim_id,
            "capability_name": assessment.capability_name,
            "policy_id": assessment.policy_id,
            "policy_allows": assessment.policy_allows,
            "active_grant_ids": list(assessment.active_grant_ids),
            "capability_status": assessment.capability_status,
            "authority_result": assessment.authority_result,
            "reason_code": assessment.reason_code,
            "execution_allowed": assessment.execution_allowed,
            "approval_required": assessment.approval_required,
            "provider_instance_id": assessment.provider_instance_id,
            "assessment_status": assessment.assessment_status,
            "execution_intent_id": assessment.execution_intent_id,
        },
        policy_version_id=assessment.policy_version_id,
        origin="personal_context",
        tenant_id=assessment.tenant_id,
        created_at=assessment.evaluated_at,
    )


def evaluate_accepted_recommendation_authority(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
    recommendation_id: str,
    now: datetime | None = None,
) -> RecommendationAuthorityAssessment:
    """Revalidate authority and optionally prepare one inert ExecutionIntent.

    ACCEPTED recommendation state is evidence of user preference, not a grant.
    This function deliberately calls the capability resolver with
    owner_authorized=False.
    """

    stamp = _utc(now or datetime.now(UTC))
    _actor, recommendation_claim = _accepted_recommendation(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        recommendation_id=recommendation_id,
        now=stamp,
    )

    policy_id, policy_version_id, policy_allows, policy_reason = _resolved_policy(
        session,
        tenant_id=tenant_id,
        actor_key=actor_key,
        recommendation_claim=recommendation_claim,
    )
    if policy_id is None or policy_version_id is None:
        assessment = RecommendationAuthorityAssessment(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            recommendation_claim_id=recommendation_claim.id,
            capability_name=REMINDER_CAPABILITY,
            policy_id=None,
            policy_version_id=None,
            policy_allows=False,
            active_grant_ids=(),
            capability_status="UNRESOLVED",
            authority_result="UNAVAILABLE",
            reason_code=policy_reason,
            execution_allowed=False,
            approval_required=False,
            provider_instance_id=None,
            assessment_status="POLICY_UNRESOLVED",
            execution_intent_id=None,
            evaluated_at=stamp,
        )
        _audit_assessment(session, assessment)
        session.flush()
        return assessment

    definition, version = capability_and_version(
        session,
        tenant_id,
        REMINDER_CAPABILITY,
    )
    if definition is not None and version is None:
        assessment = RecommendationAuthorityAssessment(
            tenant_id=tenant_id,
            actor_id=actor_key,
            recommendation_id=recommendation_id,
            recommendation_claim_id=recommendation_claim.id,
            capability_name=REMINDER_CAPABILITY,
            policy_id=policy_id,
            policy_version_id=policy_version_id,
            policy_allows=policy_allows,
            active_grant_ids=(),
            capability_status="KNOWN_BUT_UNAVAILABLE",
            authority_result="UNAVAILABLE",
            reason_code="CAPABILITY_VERSION_MISSING",
            execution_allowed=False,
            approval_required=False,
            provider_instance_id=None,
            assessment_status="CAPABILITY_UNAVAILABLE",
            execution_intent_id=None,
            evaluated_at=stamp,
        )
        _audit_assessment(session, assessment)
        session.flush()
        return assessment

    grant_ids = (
        _active_grants(
            session,
            tenant_id=tenant_id,
            actor_key=actor_key,
            capability_id=definition.id,
            now=stamp,
        )
        if definition is not None
        else ()
    )

    value = recommendation_claim.object_json or {}
    request = CapabilityRequest(
        capability=REMINDER_CAPABILITY,
        parameters=dict(value["suggested_parameters"]),
        user_requested=True,
        confidence="high",
    )
    resolution = resolve_capability_request(
        session,
        request,
        tenant_id=tenant_id,
        grantee_type="ACTOR",
        grantee_id=actor_key,
        policy_allows=policy_allows,
        owner_authorized=False,
    )

    intent_id: str | None = None
    assessment_status = "DENIED"
    if resolution.status == CapabilityResolutionStatus.REQUIRES_APPROVAL:
        assessment_status = "REQUIRES_APPROVAL"
    elif resolution.status in {
        CapabilityResolutionStatus.UNKNOWN,
        CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE,
    }:
        assessment_status = "CAPABILITY_UNAVAILABLE"
    elif resolution.execution_allowed:
        if not grant_ids:
            raise RecommendationAuthorityError(
                "RECOMMENDATION_AUTHORITY_RESOLVER_GRANT_MISMATCH"
            )
        scope = _intent_scope(
            tenant_id=tenant_id,
            actor_key=actor_key,
            recommendation_id=recommendation_id,
            recommendation_claim=recommendation_claim,
            policy_id=policy_id,
            policy_version_id=policy_version_id,
            grant_ids=grant_ids,
            capability_status=resolution.status.value,
            authority_result=resolution.authority_result,
            provider_instance_id=resolution.provider_instance_id,
            capability_version_id=version.id,
            approval_required=resolution.approval_required,
        )
        scope_fingerprint = stable_hash(scope)
        idempotency_key = _intent_idempotency_key(scope=scope)
        existing = session.scalar(
            select(ExecutionIntentRow).where(
                ExecutionIntentRow.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if (
                existing.scope_fingerprint != scope_fingerprint
                or existing.scope != scope
            ):
                raise RecommendationAuthorityError(
                    "RECOMMENDATION_EXECUTION_INTENT_IDEMPOTENCY_CONFLICT"
                )
            intent = existing
        else:
            intent = ExecutionIntentRow(
                id=new_id(),
                idempotency_key=idempotency_key,
                scope=scope,
                scope_fingerprint=scope_fingerprint,
                provenance={
                    "origin": "PERSONAL_CONTEXT_RECOMMENDATION",
                    "recommendation_claim_id": recommendation_claim.id,
                    "acceptance_event_id": (
                        recommendation_claim.context or {}
                    ).get("resolution_inbound_event_id"),
                    "policy_id": policy_id,
                    "policy_version_id": policy_version_id,
                    "grant_ids": list(grant_ids),
                    "capability_reason_code": resolution.reason_code,
                    "capability_version_id": version.id,
                    "authority_evaluated_at": stamp.isoformat(),
                },
                state="PREPARED",
                created_at=stamp,
                frozen_at=None,
                retired_at=None,
                authority_profile_id=None,
                expires_at=recommendation_claim.valid_until,
            )
            session.add(intent)
            session.flush()
        intent_id = intent.id
        assessment_status = "INTENT_PREPARED"

    assessment = RecommendationAuthorityAssessment(
        tenant_id=tenant_id,
        actor_id=actor_key,
        recommendation_id=recommendation_id,
        recommendation_claim_id=recommendation_claim.id,
        capability_name=REMINDER_CAPABILITY,
        policy_id=policy_id,
        policy_version_id=policy_version_id,
        policy_allows=policy_allows,
        active_grant_ids=grant_ids,
        capability_status=resolution.status.value,
        authority_result=resolution.authority_result,
        reason_code=resolution.reason_code,
        execution_allowed=resolution.execution_allowed,
        approval_required=resolution.approval_required,
        provider_instance_id=resolution.provider_instance_id,
        assessment_status=assessment_status,
        execution_intent_id=intent_id,
        evaluated_at=stamp,
    )
    _audit_assessment(session, assessment)
    session.flush()
    return assessment


__all__ = [
    "EXECUTION_SCOPE_VERSION",
    "RecommendationAuthorityAssessment",
    "RecommendationAuthorityError",
    "evaluate_accepted_recommendation_authority",
]
