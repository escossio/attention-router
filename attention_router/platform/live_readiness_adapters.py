"""Read-only concrete adapters for the live readiness collector.

The adapters deliberately return UNKNOWN when a platform subsystem has no
published readiness/read model.  Readiness must never be manufactured from a
missing source.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.application.platform.registry import capability_and_version, resolve_provider
from attention_router.domain.policies import resolve_policy
from attention_router.infrastructure.repository import list_policies
from attention_router.application.platform.disclosure import evaluate_disclosure_authority
from attention_router.application.agents.readiness import get_andy_readiness
from attention_router.application.platform.pre_run_context import (
    CanonicalPreRunExecutionContext,
    resolve_pre_run_execution_context,
)
from attention_router.infrastructure.models import ActorBindingRow, EntityStateRow
from attention_router.platform.readiness import DependencyRelation, DependencySignal, ReadinessState, _utc
from attention_router.platform.scenario_readiness import ScenarioReadinessContext, readiness_horizon
from attention_router.platform.standing_directives import resolve_effective_standing_directives
from attention_router.platform.execution_contract import ContractState, ExecutionRuntimeContract, get_execution_runtime_contract


def _signal(context: ScenarioReadinessContext, requirement: str, state: ReadinessState,
            reason: str, *, until: datetime | None = None) -> DependencySignal:
    horizon = readiness_horizon(context)
    effective_until = horizon if until is None else min(_utc(until), horizon)
    return DependencySignal(requirement, state, DependencyRelation.MANDATORY, context.now,
                            effective_until, reason)


class ConcreteAdapter:
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        raise NotImplementedError


class DatabasePresenceAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        session: Session | None = context.services.get("session")
        if session is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "PRESENCE_SOURCE_UNAVAILABLE")
        owner = session.scalar(select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == context.tenant_id,
            func.lower(ActorBindingRow.actor_category) == "owner",
            ActorBindingRow.is_active.is_(True),
        ).order_by(ActorBindingRow.id))
        if owner is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "OWNER_TARGET_UNRESOLVED")
        row = session.scalar(select(EntityStateRow).where(
            EntityStateRow.tenant_id == context.tenant_id,
            EntityStateRow.subject_type == "ACTOR",
            EntityStateRow.subject_id == owner.actor_key,
            EntityStateRow.state_namespace == "presence",
            EntityStateRow.state_key == "effective",
        ))
        if row is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "PRESENCE_MISSING")
        state = ReadinessState.READY
        reason = "PRESENCE_RESOLVED"
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=context.now.tzinfo)
        if expires_at is not None and context.now >= expires_at:
            state, reason = ReadinessState.STALE, "PRESENCE_STALE"
        until = expires_at or context.now + timedelta(seconds=1)
        if "SLEEPING" in requirement and row.state_value.get("status") != "sleeping":
            state, reason = ReadinessState.BLOCKED, "PRESENCE_STATE_MISMATCH"
        return _signal(context, requirement, state, reason, until=until)


class DatabaseStandingDirectiveAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        session: Session | None = context.services.get("session")
        if session is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "DIRECTIVE_SOURCE_UNAVAILABLE")
        owner = session.scalar(select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == context.tenant_id,
            func.lower(ActorBindingRow.actor_category) == "owner", ActorBindingRow.is_active.is_(True),
        ).order_by(ActorBindingRow.id))
        rows = resolve_effective_standing_directives(
            session, tenant_id=context.tenant_id, subject_actor_id=owner.actor_key if owner else "",
            trigger_type="INBOUND_MESSAGE", audience="synthetic_test", now=context.now,
        )
        row = next((item for item in rows if item.effect_type == "DISCLOSE_CURRENT_PRESENCE"), None)
        if row is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "STANDING_DIRECTIVE_MISSING")
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=context.now.tzinfo)
        return _signal(context, requirement, ReadinessState.READY, "STANDING_DIRECTIVE_RESOLVED",
                       until=expires_at or context.now + timedelta(seconds=1))


class DatabaseCapabilityProviderAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        session: Session | None = context.services.get("session")
        if session is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "CAPABILITY_SOURCE_UNAVAILABLE")
        capability = "conversation.reply"
        definition, version = capability_and_version(session, context.tenant_id, capability)
        if definition is None or version is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "CAPABILITY_MISSING")
        try:
            provider = resolve_provider(session, tenant_id=context.tenant_id, capability=definition, version=version)
        except Exception:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "PROVIDER_SOURCE_ERROR")
        if definition.availability_state not in {"PROVISIONED", "SANDBOX_PROVED", "OPERATIONAL"}:
            return _signal(context, requirement, ReadinessState.BLOCKED, "CAPABILITY_NOT_OPERATIONAL")
        if version.required_provider_interface and provider.reason_code != "PROVIDER_RESOLVED":
            return _signal(context, requirement, ReadinessState.BLOCKED, provider.reason_code)
        return _signal(context, requirement, ReadinessState.READY, "CAPABILITY_PROVIDER_RESOLVED",
                       until=context.now + timedelta(seconds=1))


class AndyReadinessAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        result = get_andy_readiness(now=context.now)
        state = ReadinessState(result.state) if result.state in ReadinessState._value2member_map_ else ReadinessState.UNKNOWN
        return _signal(context, requirement, state, result.reason_code, until=context.now + timedelta(seconds=1))


def _pre_run_context(context: ScenarioReadinessContext) -> CanonicalPreRunExecutionContext | None:
    supplied = context.services.get("pre_run_context")
    if isinstance(supplied, CanonicalPreRunExecutionContext):
        return supplied
    session: Session | None = context.services.get("session")
    if session is None:
        return None
    return resolve_pre_run_execution_context(
        session, tenant_id=context.tenant_id, scenario_id=context.scenario_id,
        scenario_version=context.scenario_version,
        synthetic_actor_key=str(context.services.get("synthetic_actor_key", "actor_synthetic_test_actor")),
        now=context.now,
    )


def resolve_canonical_disclosure_sources(
    context: ScenarioReadinessContext,
    pre_run: CanonicalPreRunExecutionContext,
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    """Read policy and represented-subject state for disclosure evaluation.

    This deliberately has no permissive fallback: an unresolved policy or
    state is represented as an empty source and the disclosure primitive
    remains fail-closed.
    """
    session: Session | None = context.services.get("session")
    if session is None or pre_run.synthetic_actor is None or pre_run.owner_actor is None:
        return (), ()

    allowed: tuple[str, ...] = ()
    try:
        resolution = resolve_policy(
            list_policies(session, context.tenant_id),
            pre_run.synthetic_actor.entity_id,
            pre_run.relationship.relationship_type or "UNKNOWN",
            None,
            audience=pre_run.audience.audience,
        )
        allowed = tuple(resolution.winner.allowed_disclosures)
    except (KeyError, ValueError, TypeError):
        pass

    row = session.scalar(select(EntityStateRow).where(
        EntityStateRow.tenant_id == context.tenant_id,
        EntityStateRow.subject_type == pre_run.owner_actor.entity_type,
        EntityStateRow.subject_id == pre_run.owner_actor.entity_id,
        EntityStateRow.state_namespace == "presence",
        EntityStateRow.state_key == "effective",
    ))
    if row is None:
        return allowed, ()
    effective_at = _utc(row.effective_at)
    expires_at = _utc(row.expires_at) if row.expires_at is not None else None
    if effective_at > context.now or (expires_at is not None and context.now >= expires_at):
        return allowed, ()
    return allowed, ({
        "namespace": row.state_namespace,
        "key": row.state_key,
        "value": row.state_value,
        "source": row.source,
        "effective_at": effective_at,
        "expires_at": expires_at,
    },)


class PreRunContextAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        value = _pre_run_context(context)
        if value is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "PRE_RUN_CONTEXT_SOURCE_UNAVAILABLE")
        checks = {
            "SYNTHETIC_ACTOR_READY": value.synthetic_actor_ready,
            "SYNTHETIC_ACTOR_NOT_OWNER": value.synthetic_actor_not_owner,
            "OWNER_TARGET_RESOLVED": value.owner_target_resolved,
        }
        if not checks.get(requirement, False):
            return _signal(context, requirement, ReadinessState.BLOCKED, value.reason_code)
        return _signal(context, requirement, ReadinessState.READY, "PRE_RUN_CONTEXT_RESOLVED")


class DisclosureAuthorityAdapter(ConcreteAdapter):
    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        value = _pre_run_context(context)
        if value is None or value.owner_actor is None or value.synthetic_actor is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "DISCLOSURE_CONTEXT_UNAVAILABLE")
        try:
            allowed_disclosures, operational_state = resolve_canonical_disclosure_sources(context, value)
            directives = context.services.get("disclosure_directives")
            if directives is None:
                session: Session | None = context.services.get("session")
                directives = resolve_effective_standing_directives(
                    session, tenant_id=context.tenant_id, subject_actor_id=value.owner_actor.entity_id,
                    trigger_type="INBOUND_MESSAGE", audience=value.audience.audience,
                    relationship=value.relationship.relationship_type, now=context.now,
                ) if session is not None else ()
            result = evaluate_disclosure_authority(
                directives=directives,
                allowed_disclosures=allowed_disclosures,
                operational_state=operational_state,
                now=context.now,
            )
        except Exception:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "DISCLOSURE_SOURCE_ERROR")
        state = ReadinessState.READY if result.allowed else ReadinessState.BLOCKED
        return _signal(context, requirement, state, result.reason_code)


class ExecutionContractAdapter(ConcreteAdapter):
    _fields = {
        "PRODUCTION_EXECUTION_LEASE_READY": "lease",
        "EXACTLY_ONCE_READY": "logical_effect_uniqueness",
        "CORRELATION_READY": "correlation_propagation",
    }

    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        contract = context.services.get("execution_runtime_contract")
        if contract is None:
            contract = get_execution_runtime_contract()
        if not isinstance(contract, ExecutionRuntimeContract):
            return _signal(context, requirement, ReadinessState.UNKNOWN, "EXECUTION_CONTRACT_MALFORMED")
        evidence = getattr(contract, self._fields[requirement], None)
        if evidence is None or evidence.state is ContractState.UNKNOWN:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "EXECUTION_CONTRACT_UNKNOWN")
        state = ReadinessState.READY if evidence.state is ContractState.SUPPORTED else ReadinessState.BLOCKED
        return _signal(context, requirement, state, evidence.reason_code)


class TransportReadinessAdapter(ConcreteAdapter):
    def __init__(self, *, role: str) -> None:
        self.role = role

    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        probe = context.services.get(f"{self.role}_transport_status")
        if probe is None:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "TRANSPORT_STATUS_SOURCE_UNAVAILABLE")
        try:
            snapshot = probe() if callable(probe) else probe
            if snapshot.fresh_until <= context.now:
                return _signal(context, requirement, ReadinessState.STALE, "TRANSPORT_STATUS_STALE", until=snapshot.fresh_until)
            state = ReadinessState.READY if snapshot.ready else ReadinessState.UNKNOWN
            return _signal(context, requirement, state, snapshot.reason_code, until=snapshot.fresh_until)
        except Exception:
            return _signal(context, requirement, ReadinessState.UNKNOWN, "TRANSPORT_STATUS_SOURCE_ERROR")


class UnavailableConcreteAdapter(ConcreteAdapter):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
        return _signal(context, requirement, ReadinessState.UNKNOWN, self.reason)


def build_default_live_readiness_adapters() -> dict[str, ConcreteAdapter]:
    """Build the production registry; unavailable subsystems fail closed."""
    registry: dict[str, ConcreteAdapter] = {}
    for key in ("OWNER_PRESENCE_SLEEPING", "OWNER_PRESENCE_FRESH"):
        registry[key] = DatabasePresenceAdapter()
    registry["STANDING_DIRECTIVE_ACTIVE"] = DatabaseStandingDirectiveAdapter()
    registry["RESPOND_PROVIDER_READY"] = DatabaseCapabilityProviderAdapter()
    registry["ANDY_CONTEXT_READY"] = AndyReadinessAdapter()
    registry["SYNTHETIC_WHATSAPP_READY"] = TransportReadinessAdapter(role="synthetic")
    for key in ("SYNTHETIC_ACTOR_READY", "SYNTHETIC_ACTOR_NOT_OWNER", "OWNER_TARGET_RESOLVED"):
        registry[key] = PreRunContextAdapter()
    registry["DISCLOSURE_AUTHORITY_ALLOWED"] = DisclosureAuthorityAdapter()
    for key in ("PRODUCTION_EXECUTION_LEASE_READY", "EXACTLY_ONCE_READY", "CORRELATION_READY"):
        registry[key] = ExecutionContractAdapter()
    return registry


__all__ = ["ConcreteAdapter", "build_default_live_readiness_adapters"]
