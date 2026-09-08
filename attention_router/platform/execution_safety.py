from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ActorBindingRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    ReadinessResultRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.platform.lineage import (
    LineageDenied,
    require_structurally_synthetic_binding,
    require_unambiguous_stimulus_id,
)
from attention_router.platform.privacy import sanitize_metadata
from attention_router.infrastructure.repository import audit

if TYPE_CHECKING:
    from attention_router.platform.scenarios import ScenarioManifest


class SafetyDenied(ValueError):
    """A deterministic safety boundary denied an operation."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class LeaseStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    CLAIMED = "CLAIMED"
    CONSUMED = "CONSUMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class BudgetStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    CANCELLED = "CANCELLED"


class ConsumptionState(StrEnum):
    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    CANCELLED = "CANCELLED"


class EffectDirection(StrEnum):
    STIMULUS = "STIMULUS"
    SYSTEM = "SYSTEM"


class ProviderOutcome(StrEnum):
    PROVED_NOT_SENT = "PROVED_NOT_SENT"
    SENT = "SENT"
    AMBIGUOUS = "AMBIGUOUS"


class ReplayDisposition(StrEnum):
    RETRY_SAME_LOGICAL_EFFECT = "RETRY_SAME_LOGICAL_EFFECT"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class ClaimReservationRequest:
    tenant_id: str
    lease_id: str
    budget_id: str | None
    claimant_id: str
    claimed_event_id: str | None
    logical_execution_id: str
    lease_idempotency_key: str
    logical_effect_id: str
    effect_idempotency_key: str
    effect_type: str
    direction: EffectDirection
    target_scope: str
    scenario_run_id: str | None
    provenance: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ClaimReservationResult:
    lease_id: str
    budget_id: str
    consumption_id: str
    logical_execution_id: str
    logical_effect_id: str
    lease_idempotency_key: str
    effect_idempotency_key: str
    idempotent_retry: bool
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class ConsumptionBindingResult:
    consumption_id: str
    tenant_id: str
    logical_effect_id: str
    execution_intent_id: str
    outbox_message_id: str | None
    idempotent: bool


@dataclass(frozen=True, slots=True)
class ScenarioSafetyProvisioningResult:
    scenario_run_id: str
    system_budget_id: str
    system_lease_id: str
    stimulus_budget_id: str | None
    stimulus_lease_id: str | None
    idempotent: bool


@dataclass(frozen=True, slots=True)
class SyntheticIntentReservationResult:
    scenario_run_id: str
    reservation: ClaimReservationResult
    binding: ConsumptionBindingResult


@dataclass(frozen=True, slots=True)
class DispatchSafetyInput:
    tenant_id: str
    lease_id: str
    budget_id: str
    consumption_id: str
    logical_execution_id: str
    logical_effect_id: str
    scenario_run_id: str | None
    effect_type: str
    direction: EffectDirection
    target_scope: str
    global_gate_open: bool
    policy_allowed: bool
    readiness_state: str
    transport_ready: bool
    clock_skew_within_bound: bool = True


@dataclass(frozen=True, slots=True)
class DispatchSafetyDecision:
    allowed: bool
    reason_code: str


SessionFactory = Callable[[], Session]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now(value: datetime | None = None) -> datetime:
    return _utc(value or datetime.now(UTC))


def _matches_scope(stored: str, requested: str) -> bool:
    return stored == requested


def replay_disposition(
    outcome: ProviderOutcome,
    *,
    external_effect_blind_retries: int = 0,
) -> ReplayDisposition:
    if external_effect_blind_retries != 0:
        raise ValueError("EXTERNAL_EFFECT_BLIND_RETRIES_MUST_EQUAL_ZERO")
    if outcome is ProviderOutcome.PROVED_NOT_SENT:
        return ReplayDisposition.RETRY_SAME_LOGICAL_EFFECT
    if outcome is ProviderOutcome.AMBIGUOUS:
        return ReplayDisposition.RECONCILIATION_REQUIRED
    return ReplayDisposition.DO_NOT_RETRY


def provision_scenario_execution_safety_in_transaction(
    session: Session,
    *,
    scenario_run_id: str,
    manifest: ScenarioManifest,
    readiness_max_age_seconds: int,
    lease_ttl_seconds: int,
    now: datetime | None = None,
) -> ScenarioSafetyProvisioningResult:
    """Materialize bounded safety state for a pinned, armed synthetic run."""

    timestamp = _now(now)
    if readiness_max_age_seconds <= 0 or lease_ttl_seconds <= 0:
        raise SafetyDenied("SCENARIO_SAFETY_TTL_CONFIGURATION_INVALID")
    run = session.execute(
        select(ScenarioRunRow)
        .where(ScenarioRunRow.id == scenario_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise SafetyDenied("SCENARIO_RUN_MISSING")
    version, actor = _validate_synthetic_run_context(
        session,
        run=run,
        readiness_max_age_seconds=readiness_max_age_seconds,
        now=timestamp,
        allowed_statuses={"CREATED", "VALIDATING", "ARMED"},
    )
    if manifest.scenario.id != _scenario_key(session, version):
        raise SafetyDenied("SCENARIO_MANIFEST_ID_MISMATCH")
    if manifest.scenario.version != version.version:
        raise SafetyDenied("SCENARIO_MANIFEST_VERSION_MISMATCH")
    if manifest.content_hash() != version.content_hash:
        raise SafetyDenied("SCENARIO_MANIFEST_HASH_MISMATCH")
    if manifest.scenario.effect_class.value != "SYNTHETIC_EXTERNAL_EFFECT":
        raise SafetyDenied("SCENARIO_EXTERNAL_EFFECT_CONTRACT_REQUIRED")
    if not manifest.safety.execution_lease.required:
        raise SafetyDenied("SCENARIO_EXECUTION_LEASE_REQUIRED")
    target_ref = manifest.safety.allowed_target_scope.get("actor_ref")
    # The manifest field names the bounded *target* reference.  OWNER is the
    # canonical target reference for the owner_sleeping scenario; the caller
    # actor is independently constrained by the structural synthetic-binding
    # checks above.  Accept actor identities as legacy target references too.
    if target_ref not in {"OWNER", "SYNTHETIC_TEST_ACTOR", actor.id, actor.actor_key}:
        raise SafetyDenied("SCENARIO_MANIFEST_ACTOR_SCOPE_MISMATCH")
    declared_effects = set(manifest.safety.effect_budget.effect_types)
    if "WHATSAPP_RESPONSE" not in declared_effects:
        raise SafetyDenied("SCENARIO_SYSTEM_RESPONSE_EFFECT_NOT_DECLARED")
    unknown_effects = declared_effects - {"WHATSAPP_STIMULUS", "WHATSAPP_RESPONSE"}
    if unknown_effects:
        raise SafetyDenied("SCENARIO_EFFECT_TYPE_UNSUPPORTED")

    target_scope = actor.external_actor_id
    valid_until = min(
        _utc(run.expires_at),
        timestamp + timedelta(seconds=manifest.safety.effect_budget.ttl_seconds),
    )
    if valid_until <= timestamp:
        raise SafetyDenied("SCENARIO_EFFECT_BUDGET_EXPIRED")
    provenance = {
        "scenario_run_id": run.id,
        "scenario_version_id": version.id,
        "scenario_hash": version.content_hash,
        "source_sha": run.source_sha,
        "runtime_sha": run.runtime_sha,
        "schema_revision": run.schema_revision,
        "driver_revision": run.driver_revision,
    }
    system_budget, system_lease, system_created = _provision_effect_input(
        session,
        run=run,
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.SYSTEM,
        limit=manifest.safety.effect_budget.max_system_response,
        target_scope=target_scope,
        lease_type=manifest.safety.execution_lease.lease_type or "SCENARIO_STEP",
        scope_key=manifest.safety.execution_lease.scope_key or run.id,
        valid_until=valid_until,
        lease_ttl_seconds=lease_ttl_seconds,
        provenance=provenance,
        now=timestamp,
    )
    stimulus_budget: EffectBudgetRow | None = None
    stimulus_lease: ExecutionLeaseRow | None = None
    stimulus_created = False
    if "WHATSAPP_STIMULUS" in declared_effects:
        stimulus_budget, stimulus_lease, stimulus_created = _provision_effect_input(
            session,
            run=run,
            effect_type="WHATSAPP_STIMULUS",
            direction=EffectDirection.STIMULUS,
            limit=manifest.safety.effect_budget.max_stimulus,
            target_scope=target_scope,
            lease_type=manifest.safety.execution_lease.lease_type or "SCENARIO_STEP",
            scope_key=manifest.safety.execution_lease.scope_key or run.id,
            valid_until=valid_until,
            lease_ttl_seconds=lease_ttl_seconds,
            provenance=provenance,
            now=timestamp,
        )
    run.effect_budget_id = system_budget.id
    run.updated_at = timestamp
    session.flush()
    return ScenarioSafetyProvisioningResult(
        scenario_run_id=run.id,
        system_budget_id=system_budget.id,
        system_lease_id=system_lease.id,
        stimulus_budget_id=stimulus_budget.id if stimulus_budget else None,
        stimulus_lease_id=stimulus_lease.id if stimulus_lease else None,
        idempotent=not system_created and not stimulus_created,
    )


def provision_production_execution_safety_in_transaction(
    session: Session,
    *,
    scenario_run_id: str,
    target_scope: str,
    valid_until: datetime,
    lease_ttl_seconds: int,
    provenance: Mapping[str, Any],
    now: datetime | None = None,
) -> ScenarioSafetyProvisioningResult:
    """Provision one inert system-reply budget/lease for a production run."""

    timestamp = _now(now)
    run = session.execute(
        select(ScenarioRunRow)
        .where(ScenarioRunRow.id == scenario_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise SafetyDenied("SCENARIO_RUN_MISSING")
    if (
        run.status != "CREATED"
        or run.agent_execution_intent_id is None
        or run.synthetic_actor_binding_id is not None
    ):
        raise SafetyDenied("PRODUCTION_RUN_CONTEXT_INVALID")
    intent = session.get(AgentExecutionIntentRow, run.agent_execution_intent_id)
    if (
        intent is None
        or intent.authorization_source != "PRODUCTION_EXECUTION_INTENT"
        or not intent.execution_intent_id
        or intent.recipient_reference != target_scope
        or intent.capability_name != "conversation.reply"
    ):
        raise SafetyDenied("PRODUCTION_EXECUTION_INTENT_INVALID")
    bounded_until = min(_utc(valid_until), _utc(run.expires_at))
    if bounded_until <= timestamp or lease_ttl_seconds <= 0:
        raise SafetyDenied("PRODUCTION_EXECUTION_WINDOW_INVALID")
    budget, lease, created = _provision_effect_input(
        session,
        run=run,
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.SYSTEM,
        limit=1,
        target_scope=target_scope,
        lease_type="PRODUCTION_EXECUTION",
        scope_key=intent.execution_intent_id,
        valid_until=bounded_until,
        lease_ttl_seconds=lease_ttl_seconds,
        provenance=provenance,
        now=timestamp,
    )
    run.effect_budget_id = budget.id
    run.updated_at = timestamp
    session.flush()
    return ScenarioSafetyProvisioningResult(
        scenario_run_id=run.id,
        system_budget_id=budget.id,
        system_lease_id=lease.id,
        stimulus_budget_id=None,
        stimulus_lease_id=None,
        idempotent=not created,
    )


def activate_scenario_run_for_execution(
    session: Session,
    *,
    scenario_run_id: str,
    authority: str,
    requires_bounded_authorization: bool = True,
    level: str = "L1",
    actor_scope: str = "",
    target_scope: str = "",
    capability_scope: str = "",
    effect_scope: str = "",
    now: datetime | None = None,
) -> bool:
    """Explicitly admit a prepared run to worker selection.

    Provisioning resources is deliberately inert.  This is the sole
    application boundary for CREATED -> ARMED and performs no lease claim or
    external-effect work.
    """
    timestamp = _now(now)
    if not authority or authority in {"SYNTHETIC_ACTOR", "ANDY", "PROVIDER", "TRANSPORT", "WORKER"}:
        raise SafetyDenied("ACTIVATION_AUTHORITY_DENIED")
    run = session.execute(
        select(ScenarioRunRow).where(ScenarioRunRow.id == scenario_run_id).with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise SafetyDenied("SCENARIO_RUN_MISSING")
    if run.status == "ARMED":
        return False
    if run.status != "CREATED":
        raise SafetyDenied("RUN_STATE_NOT_ACTIVATABLE")
    if run.effect_budget_id is None:
        raise SafetyDenied("EFFECT_BUDGET_MISSING")
    budget = session.execute(
        select(EffectBudgetRow).where(EffectBudgetRow.id == run.effect_budget_id).with_for_update()
    ).scalar_one_or_none()
    if budget is None or budget.scenario_run_id != run.id or budget.tenant_id != run.tenant_id:
        raise SafetyDenied("EXECUTION_SAFETY_MISSING")
    lease = session.execute(
        select(ExecutionLeaseRow).where(
            ExecutionLeaseRow.scenario_run_id == run.id,
            ExecutionLeaseRow.effect_budget_id == budget.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if lease is None or lease.status != "AVAILABLE" or lease.claim_count != 0:
        raise SafetyDenied("LEASE_NOT_AVAILABLE")
    if budget.status != "AVAILABLE" or budget.consumed_count != 0:
        raise SafetyDenied("BUDGET_EXHAUSTED")
    if requires_bounded_authorization:
        from attention_router.platform.bounded_authorization import check_bounded_authorization
        auth = session.execute(
            select(BoundedRunAuthorizationRow).where(
                BoundedRunAuthorizationRow.tenant_id == run.tenant_id,
                BoundedRunAuthorizationRow.scenario_run_id == run.id,
                BoundedRunAuthorizationRow.effect_budget_id == budget.id,
            ).with_for_update()
        ).scalar_one_or_none()
        if auth is None:
            raise SafetyDenied("AUTHORIZATION_MISSING")
        result = check_bounded_authorization(
            session,
            tenant_id=run.tenant_id,
            scenario_run_id=run.id,
            effect_budget_id=budget.id,
            level=level,
            actor_scope=actor_scope,
            target_scope=target_scope,
            capability_scope=capability_scope,
            effect_scope=effect_scope,
            now=timestamp,
        )
        if not result.allowed:
            raise SafetyDenied(result.reason_code)
        # A scenario may have several independent external effects.  The
        # primary run budget/auth check above covers the system response;
        # every additional effect envelope must also have exactly one valid
        # authorization before activation.
        all_budgets = session.scalars(select(EffectBudgetRow).where(
            EffectBudgetRow.tenant_id == run.tenant_id,
            EffectBudgetRow.scenario_run_id == run.id,
        )).all()
        for effect_budget in all_budgets:
            if effect_budget.id == budget.id:
                continue
            authorizations = session.scalars(select(BoundedRunAuthorizationRow).where(
                BoundedRunAuthorizationRow.tenant_id == run.tenant_id,
                BoundedRunAuthorizationRow.scenario_run_id == run.id,
                BoundedRunAuthorizationRow.effect_budget_id == effect_budget.id,
            )).all()
            if len(authorizations) != 1:
                raise SafetyDenied("MISSING_REQUIRED_EFFECT_AUTHORIZATION" if not authorizations else "DUPLICATE_EFFECT_AUTHORIZATION")
            effect_auth = authorizations[0]
            if (
                effect_auth.status != "ACTIVE"
                or timestamp < _utc(effect_auth.valid_from)
                or timestamp >= _utc(effect_auth.expires_at)
            ):
                raise SafetyDenied("AUTHORIZATION_REVOKED" if effect_auth.status == "REVOKED" else "AUTHORIZATION_EXPIRED")
            if effect_auth.effect_scope != effect_budget.effect_type or not all((
                effect_auth.level, effect_auth.actor_scope, effect_auth.target_scope,
                effect_auth.capability_scope, effect_auth.effect_scope,
            )):
                raise SafetyDenied("AUTHORIZATION_SCOPE_MISMATCH")
    run.status = "ARMED"
    run.armed_at = timestamp
    run.updated_at = timestamp
    audit(
        session,
        None,
        "SCENARIO_RUN_ACTIVATED",
        {"scenario_run_id": run.id, "from_state": "CREATED", "to_state": "ARMED", "authority": authority},
        correlation_id=run.root_correlation_id,
        tenant_id=run.tenant_id,
    )
    session.flush()
    return True


def reserve_synthetic_system_effect_for_intent_in_transaction(
    session: Session,
    *,
    intent: AgentExecutionIntentRow,
    readiness_max_age_seconds: int,
    now: datetime | None = None,
) -> SyntheticIntentReservationResult | None:
    """Reserve and bind one response only for structural synthetic lineage."""

    timestamp = _now(now)
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    event = session.get(InboundEventRow, decision.event_id) if decision else None
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    if decision is None or event is None or interaction is None:
        raise SafetyDenied("EXECUTION_INTENT_LINEAGE_UNRESOLVABLE")
    is_synthetic = event.lineage_classification == "SYNTHETIC"
    has_scenario = event.scenario_run_id is not None
    if not is_synthetic and not has_scenario:
        return None
    if not is_synthetic or not has_scenario:
        raise SafetyDenied("SYNTHETIC_SCENARIO_LINEAGE_INCOMPLETE")
    if event.tenant_id != interaction.tenant_id:
        raise SafetyDenied("SYNTHETIC_EVENT_INTERACTION_TENANT_MISMATCH")

    run = session.get(ScenarioRunRow, event.scenario_run_id)
    if run is None:
        raise SafetyDenied("SCENARIO_RUN_MISSING")
    version, actor = _validate_synthetic_run_context(
        session,
        run=run,
        readiness_max_age_seconds=readiness_max_age_seconds,
        now=timestamp,
        allowed_statuses={"ARMED", "RUNNING"},
    )
    if run.tenant_id != event.tenant_id:
        raise SafetyDenied("CROSS_TENANT_SYNTHETIC_SCENARIO_DENIED")
    if decision.actor_binding_id != actor.id:
        raise SafetyDenied("SYNTHETIC_INTENT_ACTOR_BINDING_MISMATCH")
    if interaction.contact_id != actor.actor_key:
        raise SafetyDenied("SYNTHETIC_INTERACTION_ACTOR_MISMATCH")
    payload = event.payload or {}
    if payload.get("scenario_id") != _scenario_key(session, version):
        raise SafetyDenied("SYNTHETIC_EVENT_SCENARIO_ID_MISMATCH")
    step_stimulus_ids: tuple[str, ...] = ()
    if event.scenario_step_run_id:
        step = session.get(ScenarioStepRunRow, event.scenario_step_run_id)
        if (
            step is None
            or step.tenant_id != run.tenant_id
            or step.scenario_run_id != run.id
            or step.step_kind != "STIMULUS"
        ):
            raise SafetyDenied("SYNTHETIC_STIMULUS_STEP_MISMATCH")
        step_stimulus_ids = (step.idempotency_key,)
    try:
        actor_metadata = require_structurally_synthetic_binding(actor)
        require_unambiguous_stimulus_id(
            event_stimulus_id=payload.get("stimulus_id"),
            binding_stimulus_id=actor_metadata.get("stimulus_id"),
            step_stimulus_ids=step_stimulus_ids,
        )
    except LineageDenied as exc:
        raise SafetyDenied(exc.reason_code) from exc
    payload_target = payload.get("external_actor_id") or payload.get("actor_id")
    if payload_target != actor.external_actor_id:
        raise SafetyDenied("SYNTHETIC_EVENT_TARGET_MISMATCH")
    if intent.recipient_reference not in {None, actor.external_actor_id}:
        raise SafetyDenied("SYNTHETIC_INTENT_TARGET_MISMATCH")
    intent.recipient_reference = actor.external_actor_id

    budget = session.execute(
        select(EffectBudgetRow).where(
            EffectBudgetRow.tenant_id == run.tenant_id,
            EffectBudgetRow.scenario_run_id == run.id,
            EffectBudgetRow.effect_type == "WHATSAPP_TEXT",
            EffectBudgetRow.target_scope == actor.external_actor_id,
        )
    ).scalar_one_or_none()
    if budget is None or run.effect_budget_id != budget.id:
        raise SafetyDenied("EFFECT_BUDGET_MISSING")
    lease = session.execute(
        select(ExecutionLeaseRow).where(
            ExecutionLeaseRow.tenant_id == run.tenant_id,
            ExecutionLeaseRow.scenario_run_id == run.id,
            ExecutionLeaseRow.effect_budget_id == budget.id,
            ExecutionLeaseRow.purpose == "SYSTEM_WHATSAPP_TEXT",
        )
    ).scalar_one_or_none()
    if lease is None:
        raise SafetyDenied("EXECUTION_LEASE_MISSING")
    logical_effect_id = f"execution:{intent.id}"
    reservation = claim_and_reserve_in_transaction(
        session,
        ClaimReservationRequest(
            tenant_id=run.tenant_id,
            lease_id=lease.id,
            budget_id=budget.id,
            claimant_id=intent.id,
            claimed_event_id=event.id,
            logical_execution_id=intent.idempotency_key,
            lease_idempotency_key=lease.idempotency_key,
            logical_effect_id=logical_effect_id,
            effect_idempotency_key=logical_effect_id,
            effect_type="WHATSAPP_TEXT",
            direction=EffectDirection.SYSTEM,
            target_scope=actor.external_actor_id,
            scenario_run_id=run.id,
            provenance={
                "scenario_version_id": version.id,
                "scenario_hash": version.content_hash,
                "source_sha": run.source_sha,
                "runtime_sha": run.runtime_sha,
                "schema_revision": run.schema_revision,
            },
        ),
        now=timestamp,
    )
    binding = bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=run.tenant_id,
        consumption_id=reservation.consumption_id,
        logical_effect_id=logical_effect_id,
        execution_intent_id=intent.id,
    )
    locked_run = session.execute(
        select(ScenarioRunRow)
        .where(ScenarioRunRow.id == run.id)
        .with_for_update()
    ).scalar_one()
    _validate_synthetic_run_context(
        session,
        run=locked_run,
        readiness_max_age_seconds=readiness_max_age_seconds,
        now=timestamp,
        allowed_statuses={"ARMED", "RUNNING"},
    )
    return SyntheticIntentReservationResult(
        scenario_run_id=run.id,
        reservation=reservation,
        binding=binding,
    )


def _scenario_key(session: Session, version: ScenarioVersionRow) -> str:
    definition = session.get(ScenarioDefinitionRow, version.scenario_definition_id)
    if definition is None or definition.tenant_id != version.tenant_id:
        raise SafetyDenied("SCENARIO_DEFINITION_PROVENANCE_MISSING")
    return definition.scenario_key


def _validate_synthetic_run_context(
    session: Session,
    *,
    run: ScenarioRunRow,
    readiness_max_age_seconds: int,
    now: datetime,
    allowed_statuses: set[str],
) -> tuple[ScenarioVersionRow, ActorBindingRow]:
    if readiness_max_age_seconds <= 0:
        raise SafetyDenied("READINESS_MAX_AGE_INVALID")
    if run.status not in allowed_statuses:
        raise SafetyDenied(f"SCENARIO_STATUS_{run.status}_DENIES_SAFETY_PROVISION")
    if now >= _utc(run.expires_at):
        raise SafetyDenied("SCENARIO_RUN_EXPIRED")
    version = session.get(ScenarioVersionRow, run.scenario_version_id)
    if (
        version is None
        or version.tenant_id != run.tenant_id
        or not version.enabled
        or not version.is_immutable
    ):
        raise SafetyDenied("SCENARIO_VERSION_PROVENANCE_INVALID")
    if run.source_sha != version.source_sha:
        raise SafetyDenied("SCENARIO_SOURCE_PROVENANCE_MISMATCH")
    if not run.runtime_sha or not run.schema_revision or not run.driver_revision:
        raise SafetyDenied("SCENARIO_RUNTIME_PROVENANCE_MISSING")
    actor = session.get(ActorBindingRow, run.synthetic_actor_binding_id)
    if actor is None or actor.tenant_id != run.tenant_id or not actor.is_active:
        raise SafetyDenied("SYNTHETIC_ACTOR_MISSING_OR_TENANT_MISMATCH")
    try:
        require_structurally_synthetic_binding(actor)
    except LineageDenied as exc:
        raise SafetyDenied(exc.reason_code) from exc
    owner_collision = session.scalar(
        select(ActorBindingRow.id).where(
            ActorBindingRow.tenant_id == run.tenant_id,
            ActorBindingRow.actor_category == "owner",
            ActorBindingRow.actor_key == actor.actor_key,
            ActorBindingRow.is_active.is_(True),
        )
    )
    if owner_collision is not None:
        raise SafetyDenied("SYNTHETIC_ACTOR_OWNER_COLLISION")
    readiness = session.get(ReadinessResultRow, run.readiness_result_id)
    if (
        readiness is None
        or readiness.tenant_id != run.tenant_id
        or readiness.dimension != "DOMAIN_READINESS"
        or not readiness.is_current
    ):
        raise SafetyDenied("SCENARIO_READINESS_MISSING_OR_INVALID")
    if not readiness.provenance:
        raise SafetyDenied("SCENARIO_READINESS_PROVENANCE_MISSING")
    evaluated_at = _utc(readiness.evaluated_at)
    fresh_until = _utc(readiness.evidence_fresh_until) if readiness.evidence_fresh_until else None
    if (
        readiness.state != "READY"
        or fresh_until is None
        or now > fresh_until
        or (now - evaluated_at).total_seconds() > readiness_max_age_seconds
    ):
        raise SafetyDenied("SCENARIO_READINESS_NOT_FRESH_READY")
    return version, actor


def _provision_effect_input(
    session: Session,
    *,
    run: ScenarioRunRow,
    effect_type: str,
    direction: EffectDirection,
    limit: int,
    target_scope: str,
    lease_type: str,
    scope_key: str,
    valid_until: datetime,
    lease_ttl_seconds: int,
    provenance: Mapping[str, Any],
    now: datetime,
) -> tuple[EffectBudgetRow, ExecutionLeaseRow, bool]:
    if limit < 0:
        raise SafetyDenied("SCENARIO_EFFECT_LIMIT_INVALID")
    suffix = hashlib.sha256(
        f"{run.id}\x1f{effect_type}\x1f{target_scope}".encode("utf-8")
    ).hexdigest()[:40]
    budget_id = f"bgt_{suffix}"
    lease_id = f"lse_{suffix}"
    expected_stimulus = limit if direction is EffectDirection.STIMULUS else 0
    expected_system = limit if direction is EffectDirection.SYSTEM else 0
    budget = session.get(EffectBudgetRow, budget_id)
    lease = session.get(ExecutionLeaseRow, lease_id)
    if (budget is None) != (lease is None):
        raise SafetyDenied("SCENARIO_SAFETY_PROVISIONING_STATE_INCONSISTENT")
    if budget is not None and lease is not None:
        if (
            budget.tenant_id != run.tenant_id
            or budget.scenario_run_id != run.id
            or budget.effect_type != effect_type
            or budget.target_scope != target_scope
            or budget.stimulus_limit != expected_stimulus
            or budget.system_effect_limit != expected_system
            or lease.tenant_id != run.tenant_id
            or lease.scenario_run_id != run.id
            or lease.effect_budget_id != budget.id
            or lease.lease_type != lease_type
        ):
            raise SafetyDenied("SCENARIO_SAFETY_PROVISIONING_IDENTITY_MISMATCH")
        if _utc(budget.valid_until) <= now or _utc(lease.expires_at) <= now:
            raise SafetyDenied("SCENARIO_SAFETY_PROVISIONING_EXPIRED")
        return budget, lease, False

    budget = EffectBudgetRow(
        id=budget_id,
        tenant_id=run.tenant_id,
        scenario_run_id=run.id,
        effect_type=effect_type,
        target_scope=target_scope,
        stimulus_limit=expected_stimulus,
        system_effect_limit=expected_system,
        reserved_count=0,
        consumed_count=0,
        valid_from=now,
        valid_until=valid_until,
        status=BudgetStatus.AVAILABLE.value,
        provenance=sanitize_metadata(provenance).value,
        version=1,
        created_at=now,
        updated_at=now,
    )
    lease_expires_at = min(
        valid_until,
        now + timedelta(seconds=lease_ttl_seconds),
    )
    if lease_expires_at <= now:
        raise SafetyDenied("SCENARIO_EXECUTION_LEASE_EXPIRED")
    bounded_scope = f"{scope_key}:{direction.value}:{effect_type}:{suffix[:12]}"
    lease = ExecutionLeaseRow(
        id=lease_id,
        tenant_id=run.tenant_id,
        lease_type=lease_type,
        purpose=f"{direction.value}_{effect_type}",
        scope_key=bounded_scope[:180],
        correlation_id=run.root_correlation_id,
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        claimant_id=None,
        status=LeaseStatus.AVAILABLE.value,
        not_before=now,
        expires_at=lease_expires_at,
        claimed_at=None,
        consumed_at=None,
        failed_at=None,
        cancelled_at=None,
        claim_count=0,
        max_claims=1,
        claimed_event_id=None,
        logical_execution_id=None,
        effect_budget_id=budget.id,
        idempotency_key=f"scenario:{run.id}:{direction.value}:{effect_type}"[:180],
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add_all([budget, lease])
    session.flush()
    return budget, lease, True


class ExecutionSafetyService:
    """PostgreSQL-backed one-shot lease and effect-budget arbitration.

    Public methods own a short transaction and never perform network work.  A
    caller must leave this method, then dispatch through the existing durable
    ExecutionIntent/Outbox path.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def claim_and_reserve(
        self,
        request: ClaimReservationRequest,
        *,
        now: datetime | None = None,
    ) -> ClaimReservationResult:
        try:
            with self._session_factory() as session, session.begin():
                return claim_and_reserve_in_transaction(session, request, now=now)
        except SafetyDenied as exc:
            if exc.reason_code == "LEASE_EXPIRED":
                self.expire_stale_lease(
                    tenant_id=request.tenant_id,
                    lease_id=request.lease_id,
                    now=now,
                )
            raise

    def expire_stale_lease(
        self,
        *,
        tenant_id: str,
        lease_id: str,
        now: datetime | None = None,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            return expire_stale_lease_in_transaction(
                session,
                tenant_id=tenant_id,
                lease_id=lease_id,
                now=now,
            )

    def bind_reserved_consumption_to_execution_intent(
        self,
        *,
        tenant_id: str,
        consumption_id: str,
        logical_effect_id: str,
        execution_intent_id: str,
    ) -> ConsumptionBindingResult:
        with self._session_factory() as session, session.begin():
            return bind_reserved_consumption_to_execution_intent_in_transaction(
                session,
                tenant_id=tenant_id,
                consumption_id=consumption_id,
                logical_effect_id=logical_effect_id,
                execution_intent_id=execution_intent_id,
            )

    def bind_reserved_consumption_to_outbox_message(
        self,
        *,
        tenant_id: str,
        consumption_id: str,
        logical_effect_id: str,
        execution_intent_id: str,
        outbox_message_id: str,
    ) -> ConsumptionBindingResult:
        with self._session_factory() as session, session.begin():
            return bind_reserved_consumption_to_outbox_message_in_transaction(
                session,
                tenant_id=tenant_id,
                consumption_id=consumption_id,
                logical_effect_id=logical_effect_id,
                execution_intent_id=execution_intent_id,
                outbox_message_id=outbox_message_id,
            )

    def validate_dispatch_and_bind_outbox(
        self,
        request: DispatchSafetyInput,
        *,
        execution_intent_id: str,
        outbox_message_id: str,
        now: datetime | None = None,
    ) -> ConsumptionBindingResult:
        with self._session_factory() as session, session.begin():
            return validate_dispatch_and_bind_outbox_in_transaction(
                session,
                request,
                execution_intent_id=execution_intent_id,
                outbox_message_id=outbox_message_id,
                now=now,
            )

    def finalize_consumed(
        self,
        *,
        tenant_id: str,
        lease_id: str,
        consumption_id: str,
        logical_execution_id: str,
        logical_effect_id: str,
        now: datetime | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            finalize_consumed_in_transaction(
                session,
                tenant_id=tenant_id,
                lease_id=lease_id,
                consumption_id=consumption_id,
                logical_execution_id=logical_execution_id,
                logical_effect_id=logical_effect_id,
                now=now,
            )

    def evaluate_dispatch(
        self,
        request: DispatchSafetyInput,
        *,
        now: datetime | None = None,
    ) -> DispatchSafetyDecision:
        with self._session_factory() as session, session.begin():
            return evaluate_dispatch_in_transaction(session, request, now=now)


def claim_and_reserve_in_transaction(
    session: Session,
    request: ClaimReservationRequest,
    *,
    now: datetime | None = None,
) -> ClaimReservationResult:
    """Claim a lease and reserve one budget slot in the caller's transaction."""

    timestamp = _now(now)
    if request.budget_id is None:
        raise SafetyDenied("EFFECT_BUDGET_MISSING")

    lease = session.execute(
        select(ExecutionLeaseRow)
        .where(ExecutionLeaseRow.id == request.lease_id)
        .with_for_update()
    ).scalar_one_or_none()
    if lease is None:
        raise SafetyDenied("EXECUTION_LEASE_MISSING")
    _validate_lease_scope(lease, request, timestamp)

    budget = session.execute(
        select(EffectBudgetRow)
        .where(EffectBudgetRow.id == request.budget_id)
        .with_for_update()
    ).scalar_one_or_none()
    if budget is None:
        raise SafetyDenied("EFFECT_BUDGET_MISSING")
    _validate_budget_scope(budget, request, timestamp)

    existing = session.execute(
        select(EffectConsumptionRow).where(
            EffectConsumptionRow.effect_budget_id == budget.id,
            EffectConsumptionRow.logical_effect_id == request.logical_effect_id,
        )
    ).scalar_one_or_none()

    if lease.status == LeaseStatus.CLAIMED.value:
        _validate_idempotent_claim(lease, request)
        if existing is None:
            raise SafetyDenied("CLAIM_RESERVATION_DURABLE_STATE_INCONSISTENT")
        _validate_idempotent_consumption(existing, request)
        return _reservation_result(lease, budget, existing, idempotent_retry=True)
    if lease.status != LeaseStatus.AVAILABLE.value:
        raise SafetyDenied(f"LEASE_STATUS_{lease.status}_DENIES")
    if existing is not None:
        raise SafetyDenied("LOGICAL_EFFECT_ALREADY_RESERVED_WITHOUT_MATCHING_CLAIM")

    direction_count = session.scalar(
        select(func.count())
        .select_from(EffectConsumptionRow)
        .where(
            EffectConsumptionRow.effect_budget_id == budget.id,
            EffectConsumptionRow.direction == request.direction.value,
            EffectConsumptionRow.state.in_(
                [ConsumptionState.RESERVED.value, ConsumptionState.CONSUMED.value]
            ),
        )
    )
    limit = (
        budget.stimulus_limit
        if request.direction is EffectDirection.STIMULUS
        else budget.system_effect_limit
    )
    if limit <= 0:
        raise SafetyDenied("EFFECT_BUDGET_ZERO")
    if int(direction_count or 0) >= limit:
        raise SafetyDenied("EFFECT_BUDGET_EXHAUSTED")

    if lease.claim_count >= lease.max_claims:
        raise SafetyDenied("LEASE_MAX_CLAIMS_EXHAUSTED")

    lease.status = LeaseStatus.CLAIMED.value
    lease.claimant_id = request.claimant_id
    lease.claimed_at = timestamp
    lease.claim_count += 1
    lease.claimed_event_id = request.claimed_event_id
    lease.logical_execution_id = request.logical_execution_id
    lease.effect_budget_id = budget.id
    lease.idempotency_key = request.lease_idempotency_key
    lease.version += 1
    lease.updated_at = timestamp

    consumption = EffectConsumptionRow(
        id=_effect_consumption_id(request),
        tenant_id=request.tenant_id,
        effect_budget_id=budget.id,
        logical_effect_id=request.logical_effect_id,
        direction=request.direction.value,
        target_scope=request.target_scope,
        execution_lease_id=lease.id,
        execution_intent_id=None,
        outbox_message_id=None,
        idempotency_key=request.effect_idempotency_key,
        state=ConsumptionState.RESERVED.value,
        reserved_at=timestamp,
        consumed_at=None,
        released_at=None,
        cancelled_at=None,
        provenance=sanitize_metadata(request.provenance).value,
        created_at=timestamp,
    )
    session.add(consumption)
    budget.reserved_count += 1
    budget.status = BudgetStatus.RESERVED.value
    budget.version += 1
    budget.updated_at = timestamp
    session.flush()
    return _reservation_result(lease, budget, consumption, idempotent_retry=False)


def expire_stale_lease_in_transaction(
    session: Session,
    *,
    tenant_id: str,
    lease_id: str,
    now: datetime | None = None,
) -> bool:
    """Retire an expired lease and release only a provably undispatched reservation."""

    timestamp = _now(now)
    lease = session.execute(
        select(ExecutionLeaseRow)
        .where(ExecutionLeaseRow.id == lease_id)
        .with_for_update()
    ).scalar_one_or_none()
    if lease is None:
        raise SafetyDenied("EXECUTION_LEASE_MISSING")
    if lease.tenant_id != tenant_id:
        raise SafetyDenied("CROSS_TENANT_LEASE_DENIED")
    if lease.status not in {LeaseStatus.AVAILABLE.value, LeaseStatus.CLAIMED.value}:
        return False
    if timestamp < _utc(lease.expires_at):
        return False

    reservations = session.scalars(
        select(EffectConsumptionRow).where(
            EffectConsumptionRow.execution_lease_id == lease.id,
            EffectConsumptionRow.state == ConsumptionState.RESERVED.value,
        )
    ).all()
    if len(reservations) > 1:
        raise SafetyDenied("LEASE_HAS_MULTIPLE_ACTIVE_CONSUMPTIONS")
    if reservations:
        snapshot = reservations[0]
        budget = session.execute(
            select(EffectBudgetRow)
            .where(EffectBudgetRow.id == snapshot.effect_budget_id)
            .with_for_update()
        ).scalar_one_or_none()
        if budget is None:
            raise SafetyDenied("EXPIRED_LEASE_BUDGET_MISSING")
        consumption = session.execute(
            select(EffectConsumptionRow)
            .where(EffectConsumptionRow.id == snapshot.id)
            .with_for_update()
        ).scalar_one()
        if budget.tenant_id != tenant_id:
            raise SafetyDenied("CROSS_TENANT_EXPIRED_LEASE_BUDGET_DENIED")
        if (
            consumption.state == ConsumptionState.RESERVED.value
            and consumption.outbox_message_id is None
        ):
            if budget.reserved_count <= 0:
                raise SafetyDenied("EXPIRED_LEASE_BUDGET_COUNT_INCONSISTENT")
            consumption.state = ConsumptionState.RELEASED.value
            consumption.released_at = timestamp
            budget.reserved_count -= 1
            if budget.status == BudgetStatus.RESERVED.value and budget.reserved_count == 0:
                budget.status = BudgetStatus.AVAILABLE.value
            budget.version += 1
            budget.updated_at = timestamp

    # A linked outbox is deliberately retained for reconciliation: releasing it
    # could authorize a second effect while the provider outcome is unknown.
    lease.status = LeaseStatus.EXPIRED.value
    lease.version += 1
    lease.updated_at = timestamp
    session.flush()
    return True


def bind_reserved_consumption_to_execution_intent_in_transaction(
    session: Session,
    *,
    tenant_id: str,
    consumption_id: str,
    logical_effect_id: str,
    execution_intent_id: str,
) -> ConsumptionBindingResult:
    """Bind a reservation to one exact intent without performing external work."""

    consumption, _, budget = _lock_reserved_consumption_graph(
        session,
        consumption_id=consumption_id,
        tenant_id=tenant_id,
        logical_effect_id=logical_effect_id,
    )
    intent = _lock_execution_intent(session, execution_intent_id)
    _validate_intent_effect_scope(
        session,
        intent=intent,
        consumption=consumption,
        budget=budget,
        tenant_id=tenant_id,
    )

    other = session.scalar(
        select(EffectConsumptionRow.id).where(
            EffectConsumptionRow.execution_intent_id == intent.id,
            EffectConsumptionRow.id != consumption.id,
        )
    )
    if other is not None:
        raise SafetyDenied("EXECUTION_INTENT_ALREADY_LINKED_TO_DIFFERENT_CONSUMPTION")
    if consumption.execution_intent_id is not None:
        if consumption.execution_intent_id != intent.id:
            raise SafetyDenied("CONSUMPTION_EXECUTION_INTENT_LINK_IMMUTABLE")
        return _binding_result(consumption, idempotent=True)

    consumption.execution_intent_id = intent.id
    session.flush()
    return _binding_result(consumption, idempotent=False)


def bind_reserved_consumption_to_outbox_message_in_transaction(
    session: Session,
    *,
    tenant_id: str,
    consumption_id: str,
    logical_effect_id: str,
    execution_intent_id: str,
    outbox_message_id: str,
) -> ConsumptionBindingResult:
    """Bind an intent-linked reservation to its exact durable outbox row."""

    consumption, _, budget = _lock_reserved_consumption_graph(
        session,
        consumption_id=consumption_id,
        tenant_id=tenant_id,
        logical_effect_id=logical_effect_id,
    )
    intent = _lock_execution_intent(session, execution_intent_id)
    _validate_intent_effect_scope(
        session,
        intent=intent,
        consumption=consumption,
        budget=budget,
        tenant_id=tenant_id,
    )
    if consumption.execution_intent_id != execution_intent_id:
        raise SafetyDenied("CONSUMPTION_EXECUTION_INTENT_MISMATCH")

    outbox = session.execute(
        select(OutboxMessageRow)
        .where(OutboxMessageRow.id == outbox_message_id)
        .with_for_update()
    ).scalar_one_or_none()
    if outbox is None:
        raise SafetyDenied("OUTBOX_MESSAGE_MISSING")
    outbox_tenant_id = _tenant_for_outbox_message(session, outbox)
    if outbox_tenant_id != tenant_id:
        raise SafetyDenied("CROSS_TENANT_OUTBOX_DENIED")
    if outbox.execution_intent_id != execution_intent_id:
        raise SafetyDenied("OUTBOX_EXECUTION_INTENT_MISMATCH")
    _validate_outbox_effect_scope(outbox, intent=intent, budget=budget)

    other = session.scalar(
        select(EffectConsumptionRow.id).where(
            EffectConsumptionRow.outbox_message_id == outbox.id,
            EffectConsumptionRow.id != consumption.id,
        )
    )
    if other is not None:
        raise SafetyDenied("OUTBOX_ALREADY_LINKED_TO_DIFFERENT_CONSUMPTION")
    if consumption.outbox_message_id is not None:
        if consumption.outbox_message_id != outbox.id:
            raise SafetyDenied("CONSUMPTION_OUTBOX_LINK_IMMUTABLE")
        return _binding_result(consumption, idempotent=True)

    consumption.outbox_message_id = outbox.id
    session.flush()
    return _binding_result(consumption, idempotent=False)


def _tenant_for_execution_intent(
    session: Session,
    intent: AgentExecutionIntentRow,
) -> str:
    tenant_id = session.scalar(
        select(InteractionRow.tenant_id)
        .join(AgentDecisionRow, AgentDecisionRow.interaction_id == InteractionRow.id)
        .where(AgentDecisionRow.id == intent.agent_decision_id)
    )
    if tenant_id is None:
        raise SafetyDenied("EXECUTION_INTENT_TENANT_UNRESOLVABLE")
    return tenant_id


def _lock_execution_intent(
    session: Session,
    execution_intent_id: str,
) -> AgentExecutionIntentRow:
    intent = session.execute(
        select(AgentExecutionIntentRow)
        .where(AgentExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    if intent is None:
        raise SafetyDenied("EXECUTION_INTENT_MISSING")
    return intent


def _tenant_for_outbox_message(session: Session, outbox: OutboxMessageRow) -> str:
    tenant_id = session.scalar(
        select(InteractionRow.tenant_id).where(InteractionRow.id == outbox.interaction_id)
    )
    if tenant_id is None:
        raise SafetyDenied("OUTBOX_TENANT_UNRESOLVABLE")
    return tenant_id


def _validate_reserved_consumption_identity(
    consumption: EffectConsumptionRow | None,
    *,
    tenant_id: str,
    logical_effect_id: str,
) -> None:
    if consumption is None:
        raise SafetyDenied("EFFECT_CONSUMPTION_MISSING")
    if consumption.tenant_id != tenant_id:
        raise SafetyDenied("CROSS_TENANT_CONSUMPTION_DENIED")
    if consumption.logical_effect_id != logical_effect_id:
        raise SafetyDenied("CONSUMPTION_LOGICAL_EFFECT_MISMATCH")
    if consumption.state != ConsumptionState.RESERVED.value:
        raise SafetyDenied(f"CONSUMPTION_STATUS_{consumption.state}_DENIES_LINK")


def _lock_reserved_consumption_graph(
    session: Session,
    *,
    consumption_id: str,
    tenant_id: str,
    logical_effect_id: str,
    now: datetime | None = None,
) -> tuple[EffectConsumptionRow, ExecutionLeaseRow, EffectBudgetRow]:
    snapshot = session.get(EffectConsumptionRow, consumption_id)
    if snapshot is None:
        raise SafetyDenied("EFFECT_CONSUMPTION_MISSING")
    lease = session.execute(
        select(ExecutionLeaseRow)
        .where(ExecutionLeaseRow.id == snapshot.execution_lease_id)
        .with_for_update()
    ).scalar_one_or_none()
    budget = session.execute(
        select(EffectBudgetRow)
        .where(EffectBudgetRow.id == snapshot.effect_budget_id)
        .with_for_update()
    ).scalar_one_or_none()
    consumption = session.execute(
        select(EffectConsumptionRow)
        .where(EffectConsumptionRow.id == consumption_id)
        .with_for_update()
    ).scalar_one_or_none()
    if lease is None or budget is None or consumption is None:
        raise SafetyDenied("DURABLE_SAFETY_STATE_MISSING")
    _validate_reserved_consumption_identity(
        consumption,
        tenant_id=tenant_id,
        logical_effect_id=logical_effect_id,
    )
    if {lease.tenant_id, budget.tenant_id} != {tenant_id}:
        raise SafetyDenied("CROSS_TENANT_CONSUMPTION_GRAPH_DENIED")
    if consumption.execution_lease_id != lease.id:
        raise SafetyDenied("CONSUMPTION_LEASE_MISMATCH")
    if consumption.effect_budget_id != budget.id or lease.effect_budget_id != budget.id:
        raise SafetyDenied("LEASE_BUDGET_CONSUMPTION_LINK_MISMATCH")
    if lease.status != LeaseStatus.CLAIMED.value:
        raise SafetyDenied(f"LEASE_STATUS_{lease.status}_DENIES_LINK")
    timestamp = _now(now)
    if timestamp >= _utc(lease.expires_at):
        raise SafetyDenied("LEASE_EXPIRED")
    if timestamp < _utc(budget.valid_from) or timestamp >= _utc(budget.valid_until):
        raise SafetyDenied("EFFECT_BUDGET_EXPIRED")
    if budget.status != BudgetStatus.RESERVED.value:
        raise SafetyDenied(f"BUDGET_STATUS_{budget.status}_DENIES_LINK")
    return consumption, lease, budget


_EFFECT_INTENT_TYPES: dict[str, frozenset[str]] = {
    "WHATSAPP_TEXT": frozenset({"WHATSAPP_TEXT", "WHATSAPP_RESPONSE", "WHATSAPP_STIMULUS"}),
    "WHATSAPP_RESPONSE": frozenset({"WHATSAPP_RESPONSE"}),
    "WHATSAPP_STIMULUS": frozenset({"WHATSAPP_STIMULUS"}),
}

_EFFECT_DESTINATIONS: dict[str, frozenset[str]] = {
    "WHATSAPP_TEXT": frozenset(
        {"local_transport", "synthetic_transport", "meta_whatsapp_cloud"}
    ),
    "WHATSAPP_RESPONSE": frozenset({"local_transport"}),
    "WHATSAPP_STIMULUS": frozenset({"synthetic_transport"}),
}


def _validate_intent_effect_scope(
    session: Session,
    *,
    intent: AgentExecutionIntentRow,
    consumption: EffectConsumptionRow,
    budget: EffectBudgetRow,
    tenant_id: str,
) -> None:
    if _tenant_for_execution_intent(session, intent) != tenant_id:
        raise SafetyDenied("CROSS_TENANT_EXECUTION_INTENT_DENIED")
    if not intent.recipient_reference:
        raise SafetyDenied("EXECUTION_INTENT_TARGET_MISSING")
    if not _matches_scope(consumption.target_scope, intent.recipient_reference):
        raise SafetyDenied("EXECUTION_INTENT_TARGET_SCOPE_MISMATCH")
    allowed_intent_types = _EFFECT_INTENT_TYPES.get(
        budget.effect_type,
        frozenset({budget.effect_type}),
    )
    if intent.intent_type not in allowed_intent_types:
        raise SafetyDenied("EXECUTION_INTENT_EFFECT_TYPE_MISMATCH")


def _validate_outbox_effect_scope(
    outbox: OutboxMessageRow,
    *,
    intent: AgentExecutionIntentRow,
    budget: EffectBudgetRow,
) -> None:
    allowed_destinations = _EFFECT_DESTINATIONS.get(budget.effect_type)
    if allowed_destinations is None or outbox.destination not in allowed_destinations:
        raise SafetyDenied("OUTBOX_EFFECT_DESTINATION_MISMATCH")
    payload = outbox.payload or {}
    payload_intent_id = payload.get("execution_intent_id")
    if payload_intent_id is not None and payload_intent_id != intent.id:
        raise SafetyDenied("OUTBOX_PAYLOAD_EXECUTION_INTENT_MISMATCH")
    payload_target = (
        payload.get("recipient_reference")
        or payload.get("external_actor_id")
        or payload.get("actor_ref")
    )
    if payload_target != intent.recipient_reference:
        raise SafetyDenied("OUTBOX_PAYLOAD_TARGET_SCOPE_MISMATCH")
    if budget.effect_type.startswith("WHATSAPP_") and payload.get("message_type") != "text":
        raise SafetyDenied("OUTBOX_EFFECT_PAYLOAD_TYPE_MISMATCH")


def _binding_result(
    consumption: EffectConsumptionRow,
    *,
    idempotent: bool,
) -> ConsumptionBindingResult:
    assert consumption.execution_intent_id is not None
    return ConsumptionBindingResult(
        consumption_id=consumption.id,
        tenant_id=consumption.tenant_id,
        logical_effect_id=consumption.logical_effect_id,
        execution_intent_id=consumption.execution_intent_id,
        outbox_message_id=consumption.outbox_message_id,
        idempotent=idempotent,
    )


def _effect_consumption_id(request: ClaimReservationRequest) -> str:
    key = f"{request.budget_id}\x1f{request.logical_effect_id}"
    return "efc_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:48]


def _validate_lease_scope(
    lease: ExecutionLeaseRow,
    request: ClaimReservationRequest,
    timestamp: datetime,
) -> None:
    if lease.tenant_id != request.tenant_id:
        raise SafetyDenied("CROSS_TENANT_LEASE_DENIED")
    if lease.scenario_run_id != request.scenario_run_id:
        raise SafetyDenied("LEASE_SCENARIO_MISMATCH")
    if lease.idempotency_key != request.lease_idempotency_key:
        raise SafetyDenied("LEASE_IDEMPOTENCY_KEY_MISMATCH")
    if lease.not_before and timestamp < _utc(lease.not_before):
        raise SafetyDenied("LEASE_NOT_YET_VALID")
    if timestamp >= _utc(lease.expires_at):
        raise SafetyDenied("LEASE_EXPIRED")
    if lease.effect_budget_id and lease.effect_budget_id != request.budget_id:
        raise SafetyDenied("LEASE_BUDGET_MISMATCH")


def _validate_idempotent_claim(
    lease: ExecutionLeaseRow,
    request: ClaimReservationRequest,
) -> None:
    if lease.claimed_event_id != request.claimed_event_id:
        raise SafetyDenied("LEASE_EVENT_MISMATCH")
    if lease.logical_execution_id != request.logical_execution_id:
        raise SafetyDenied("LEASE_LOGICAL_EXECUTION_MISMATCH")
    if lease.idempotency_key != request.lease_idempotency_key:
        raise SafetyDenied("LEASE_IDEMPOTENCY_KEY_MISMATCH")
    if lease.claimant_id != request.claimant_id:
        raise SafetyDenied("LEASE_ALREADY_CLAIMED")


def _validate_budget_scope(
    budget: EffectBudgetRow,
    request: ClaimReservationRequest,
    timestamp: datetime,
) -> None:
    if budget.tenant_id != request.tenant_id:
        raise SafetyDenied("CROSS_TENANT_BUDGET_DENIED")
    if budget.scenario_run_id != request.scenario_run_id:
        raise SafetyDenied("BUDGET_SCENARIO_MISMATCH")
    if budget.effect_type != request.effect_type:
        raise SafetyDenied("BUDGET_EFFECT_TYPE_MISMATCH")
    if not _matches_scope(budget.target_scope, request.target_scope):
        raise SafetyDenied("BUDGET_TARGET_SCOPE_MISMATCH")
    if timestamp < _utc(budget.valid_from) or timestamp >= _utc(budget.valid_until):
        raise SafetyDenied("EFFECT_BUDGET_EXPIRED")
    if budget.status in {
        BudgetStatus.CONSUMED.value,
        BudgetStatus.RELEASED.value,
        BudgetStatus.CANCELLED.value,
    }:
        raise SafetyDenied(f"BUDGET_STATUS_{budget.status}_DENIES")


def _validate_idempotent_consumption(
    consumption: EffectConsumptionRow,
    request: ClaimReservationRequest,
) -> None:
    if consumption.tenant_id != request.tenant_id:
        raise SafetyDenied("CROSS_TENANT_CONSUMPTION_DENIED")
    if consumption.execution_lease_id != request.lease_id:
        raise SafetyDenied("CONSUMPTION_LEASE_MISMATCH")
    if consumption.idempotency_key != request.effect_idempotency_key:
        raise SafetyDenied("CONSUMPTION_IDEMPOTENCY_KEY_MISMATCH")
    if consumption.direction != request.direction.value:
        raise SafetyDenied("CONSUMPTION_DIRECTION_MISMATCH")
    if not _matches_scope(consumption.target_scope, request.target_scope):
        raise SafetyDenied("CONSUMPTION_TARGET_SCOPE_MISMATCH")
    if consumption.state not in {
        ConsumptionState.RESERVED.value,
        ConsumptionState.CONSUMED.value,
    }:
        raise SafetyDenied(f"CONSUMPTION_STATUS_{consumption.state}_DENIES")


def _reservation_result(
    lease: ExecutionLeaseRow,
    budget: EffectBudgetRow,
    consumption: EffectConsumptionRow,
    *,
    idempotent_retry: bool,
) -> ClaimReservationResult:
    return ClaimReservationResult(
        lease_id=lease.id,
        budget_id=budget.id,
        consumption_id=consumption.id,
        logical_execution_id=lease.logical_execution_id,
        logical_effect_id=consumption.logical_effect_id,
        lease_idempotency_key=lease.idempotency_key,
        effect_idempotency_key=consumption.idempotency_key,
        idempotent_retry=idempotent_retry,
        reconciliation_required=(
            idempotent_retry and consumption.outbox_message_id is not None
        ),
    )


def finalize_consumed_in_transaction(
    session: Session,
    *,
    tenant_id: str,
    lease_id: str,
    consumption_id: str,
    logical_execution_id: str,
    logical_effect_id: str,
    now: datetime | None = None,
) -> None:
    timestamp = _now(now)
    lease = session.execute(
        select(ExecutionLeaseRow)
        .where(ExecutionLeaseRow.id == lease_id)
        .with_for_update()
    ).scalar_one_or_none()
    consumption_snapshot = session.get(EffectConsumptionRow, consumption_id)
    if lease is None or consumption_snapshot is None:
        raise SafetyDenied("FINALIZATION_DURABLE_STATE_MISSING")
    budget = session.execute(
        select(EffectBudgetRow)
        .where(EffectBudgetRow.id == consumption_snapshot.effect_budget_id)
        .with_for_update()
    ).scalar_one_or_none()
    if budget is None:
        raise SafetyDenied("FINALIZATION_BUDGET_MISSING")
    consumption = session.execute(
        select(EffectConsumptionRow)
        .where(EffectConsumptionRow.id == consumption_id)
        .with_for_update()
    ).scalar_one()
    if {lease.tenant_id, consumption.tenant_id, budget.tenant_id} != {tenant_id}:
        raise SafetyDenied("CROSS_TENANT_FINALIZATION_DENIED")
    if lease.logical_execution_id != logical_execution_id:
        raise SafetyDenied("FINALIZATION_LOGICAL_EXECUTION_MISMATCH")
    if consumption.logical_effect_id != logical_effect_id:
        raise SafetyDenied("FINALIZATION_LOGICAL_EFFECT_MISMATCH")
    if consumption.execution_lease_id != lease.id:
        raise SafetyDenied("FINALIZATION_CONSUMPTION_LEASE_MISMATCH")
    if consumption.effect_budget_id != budget.id or lease.effect_budget_id != budget.id:
        raise SafetyDenied("FINALIZATION_LEASE_BUDGET_MISMATCH")
    if (
        consumption.direction == EffectDirection.SYSTEM.value
        and (not consumption.execution_intent_id or not consumption.outbox_message_id)
    ):
        raise SafetyDenied("FINALIZATION_SYSTEM_EFFECT_DELIVERY_LINK_MISSING")
    if lease.status == LeaseStatus.CONSUMED.value:
        if consumption.state != ConsumptionState.CONSUMED.value:
            raise SafetyDenied("FINALIZATION_DURABLE_STATE_INCONSISTENT")
        return
    if lease.status != LeaseStatus.CLAIMED.value:
        raise SafetyDenied(f"LEASE_STATUS_{lease.status}_DENIES_FINALIZATION")
    if consumption.state != ConsumptionState.RESERVED.value:
        raise SafetyDenied(f"CONSUMPTION_STATUS_{consumption.state}_DENIES_FINALIZATION")
    if budget.reserved_count <= 0:
        raise SafetyDenied("FINALIZATION_BUDGET_RESERVED_COUNT_INCONSISTENT")

    consumption.state = ConsumptionState.CONSUMED.value
    consumption.consumed_at = timestamp
    lease.status = LeaseStatus.CONSUMED.value
    lease.consumed_at = timestamp
    lease.version += 1
    lease.updated_at = timestamp
    budget.reserved_count -= 1
    budget.consumed_count += 1
    total_limit = budget.stimulus_limit + budget.system_effect_limit
    if budget.consumed_count >= total_limit:
        budget.status = BudgetStatus.CONSUMED.value
    elif budget.reserved_count:
        budget.status = BudgetStatus.RESERVED.value
    else:
        budget.status = BudgetStatus.AVAILABLE.value
    budget.version += 1
    budget.updated_at = timestamp
    session.flush()


def evaluate_dispatch_in_transaction(
    session: Session,
    request: DispatchSafetyInput,
    *,
    now: datetime | None = None,
) -> DispatchSafetyDecision:
    """Re-evaluate durable safety immediately before durable outbox dispatch."""

    if not request.global_gate_open:
        return DispatchSafetyDecision(False, "GLOBAL_EXTERNAL_GATE_CLOSED")
    if not request.policy_allowed:
        return DispatchSafetyDecision(False, "POLICY_DENIED")
    if request.readiness_state != "READY":
        return DispatchSafetyDecision(False, f"READINESS_{request.readiness_state}_DENIES")
    if not request.transport_ready:
        return DispatchSafetyDecision(False, "TRANSPORT_NOT_READY")
    if not request.clock_skew_within_bound:
        return DispatchSafetyDecision(False, "CLOCK_SKEW_UNSAFE")

    timestamp = _now(now)
    lease = session.execute(
        select(ExecutionLeaseRow)
        .where(ExecutionLeaseRow.id == request.lease_id)
        .with_for_update()
    ).scalar_one_or_none()
    budget = session.execute(
        select(EffectBudgetRow)
        .where(EffectBudgetRow.id == request.budget_id)
        .with_for_update()
    ).scalar_one_or_none()
    consumption = session.execute(
        select(EffectConsumptionRow)
        .where(EffectConsumptionRow.id == request.consumption_id)
        .with_for_update()
    ).scalar_one_or_none()
    if lease is None or budget is None or consumption is None:
        return DispatchSafetyDecision(False, "DURABLE_SAFETY_STATE_MISSING")
    if {lease.tenant_id, budget.tenant_id, consumption.tenant_id} != {request.tenant_id}:
        return DispatchSafetyDecision(False, "CROSS_TENANT_DISPATCH_DENIED")
    if lease.status != LeaseStatus.CLAIMED.value:
        return DispatchSafetyDecision(False, f"LEASE_STATUS_{lease.status}_DENIES")
    if timestamp >= _utc(lease.expires_at):
        return DispatchSafetyDecision(False, "LEASE_EXPIRED")
    if lease.logical_execution_id != request.logical_execution_id:
        return DispatchSafetyDecision(False, "LEASE_LOGICAL_EXECUTION_MISMATCH")
    if consumption.state != ConsumptionState.RESERVED.value:
        return DispatchSafetyDecision(False, f"CONSUMPTION_STATUS_{consumption.state}_DENIES")
    if consumption.logical_effect_id != request.logical_effect_id:
        return DispatchSafetyDecision(False, "CONSUMPTION_LOGICAL_EFFECT_MISMATCH")
    if consumption.execution_lease_id != lease.id or consumption.effect_budget_id != budget.id:
        return DispatchSafetyDecision(False, "LEASE_BUDGET_CONSUMPTION_LINK_MISMATCH")
    if timestamp < _utc(budget.valid_from) or timestamp >= _utc(budget.valid_until):
        return DispatchSafetyDecision(False, "EFFECT_BUDGET_EXPIRED")
    if budget.status in {
        BudgetStatus.CANCELLED.value,
        BudgetStatus.RELEASED.value,
        BudgetStatus.CONSUMED.value,
    }:
        return DispatchSafetyDecision(False, f"BUDGET_STATUS_{budget.status}_DENIES")
    if budget.scenario_run_id != request.scenario_run_id:
        return DispatchSafetyDecision(False, "BUDGET_SCENARIO_MISMATCH")
    if budget.effect_type != request.effect_type:
        return DispatchSafetyDecision(False, "BUDGET_EFFECT_TYPE_MISMATCH")
    if consumption.direction != request.direction.value:
        return DispatchSafetyDecision(False, "CONSUMPTION_DIRECTION_MISMATCH")
    if not _matches_scope(budget.target_scope, request.target_scope):
        return DispatchSafetyDecision(False, "BUDGET_TARGET_SCOPE_MISMATCH")
    if not _matches_scope(consumption.target_scope, request.target_scope):
        return DispatchSafetyDecision(False, "CONSUMPTION_TARGET_SCOPE_MISMATCH")
    if request.scenario_run_id:
        scenario = session.execute(
            select(ScenarioRunRow)
            .where(ScenarioRunRow.id == request.scenario_run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if scenario is None or scenario.tenant_id != request.tenant_id:
            return DispatchSafetyDecision(False, "SCENARIO_RUN_MISSING_OR_TENANT_MISMATCH")
        if timestamp >= _utc(scenario.expires_at):
            return DispatchSafetyDecision(False, "SCENARIO_RUN_EXPIRED")
        if scenario.status in {"NOT_READY", "PASSED", "FAILED", "ABORTED", "EXPIRED"}:
            return DispatchSafetyDecision(False, f"TERMINAL_SCENARIO_{scenario.status}_DENIES")
        if scenario.status not in {"ARMED", "RUNNING"}:
            return DispatchSafetyDecision(False, f"SCENARIO_STATUS_{scenario.status}_DENIES")
    return DispatchSafetyDecision(True, "DISPATCH_SAFETY_REVALIDATED")


def validate_dispatch_and_bind_outbox_in_transaction(
    session: Session,
    request: DispatchSafetyInput,
    *,
    execution_intent_id: str,
    outbox_message_id: str,
    now: datetime | None = None,
) -> ConsumptionBindingResult:
    """Atomically revalidate dispatch and bind its durable outbox identity.

    The caller must create/flush the outbox row in this same transaction.  A
    denial raises, so the caller's transaction rolls the outbox creation back.
    No provider or other network call belongs inside this boundary.
    """

    decision = evaluate_dispatch_in_transaction(session, request, now=now)
    if not decision.allowed:
        raise SafetyDenied(decision.reason_code)
    return bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=request.tenant_id,
        consumption_id=request.consumption_id,
        logical_effect_id=request.logical_effect_id,
        execution_intent_id=execution_intent_id,
        outbox_message_id=outbox_message_id,
    )
