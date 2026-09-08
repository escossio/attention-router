from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    ActorBindingRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.invariants import CANONICAL_INVARIANT_IDS
from attention_router.platform.lineage import (
    LineageDenied,
    require_structurally_synthetic_binding,
)
from attention_router.platform.privacy import sanitize_metadata


class ScenarioContractError(ValueError):
    pass


class ScenarioPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ScenarioTestLevel(StrEnum):
    L0_UNIT_DRY = "L0_UNIT_DRY"
    L1_SYNTHETIC_E2E = "L1_SYNTHETIC_E2E"
    L2_BOUNDED_HUMAN_CANARY = "L2_BOUNDED_HUMAN_CANARY"
    L3_PRODUCTION_OBSERVATION = "L3_PRODUCTION_OBSERVATION"


class ExecutorKind(StrEnum):
    PYTEST_UNIT = "PYTEST_UNIT"
    PYTEST_CONTRACT = "PYTEST_CONTRACT"
    POSTGRES_HARNESS = "POSTGRES_HARNESS"
    CONCURRENCY_HARNESS = "CONCURRENCY_HARNESS"
    SCENARIO_ENGINE = "SCENARIO_ENGINE"
    SYNTHETIC_DRIVER = "SYNTHETIC_DRIVER"
    FAULT_INJECTION_HARNESS = "FAULT_INJECTION_HARNESS"
    SECURITY_NEGATIVE_HARNESS = "SECURITY_NEGATIVE_HARNESS"
    PROVENANCE_RECONCILER = "PROVENANCE_RECONCILER"
    HUMAN_CANARY = "HUMAN_CANARY"
    PRODUCTION_OBSERVER = "PRODUCTION_OBSERVER"


class EffectClass(StrEnum):
    NO_EXTERNAL_EFFECT = "NO_EXTERNAL_EFFECT"
    INTERNAL_STATE_EFFECT = "INTERNAL_STATE_EFFECT"
    SYNTHETIC_EXTERNAL_EFFECT = "SYNTHETIC_EXTERNAL_EFFECT"
    HUMAN_CANARY_EXTERNAL_EFFECT = "HUMAN_CANARY_EXTERNAL_EFFECT"
    PRODUCTION_OBSERVATION_ONLY = "PRODUCTION_OBSERVATION_ONLY"


class ScenarioRunStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    NOT_READY = "NOT_READY"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


class ScenarioStepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ResumeDisposition(StrEnum):
    NO_ACTIVE_STEP = "NO_ACTIVE_STEP"
    RESUME_EXISTING_STEP = "RESUME_EXISTING_STEP"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    TERMINAL = "TERMINAL"
    EXPIRED = "EXPIRED"


TERMINAL_RUN_STATUSES = frozenset(
    {
        ScenarioRunStatus.PASSED.value,
        ScenarioRunStatus.FAILED.value,
        ScenarioRunStatus.ABORTED.value,
        ScenarioRunStatus.EXPIRED.value,
    }
)

TERMINAL_STEP_STATUSES = frozenset(
    {
        ScenarioStepStatus.PASSED.value,
        ScenarioStepStatus.FAILED.value,
        ScenarioStepStatus.SKIPPED.value,
        ScenarioStepStatus.ABORTED.value,
        ScenarioStepStatus.EXPIRED.value,
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioIdentity(StrictModel):
    id: str = Field(pattern=r"^SCN-PE-[0-9]{3}$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    priority: ScenarioPriority
    level: ScenarioTestLevel
    executor_kind: ExecutorKind
    effect_class: EffectClass = EffectClass.NO_EXTERNAL_EFFECT


class ScenarioTraceability(StrictModel):
    pe_ids: tuple[str, ...] = ()
    acceptance_ids: tuple[str, ...] = ()
    invariant_ids: tuple[str, ...] = ()
    risk_ids: tuple[str, ...] = ()
    historical_risk_ids: tuple[str, ...] = ()
    coverage_gap_ids: tuple[str, ...] = ()
    work_packages: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_invariants(self) -> ScenarioTraceability:
        unknown = sorted(set(self.invariant_ids) - CANONICAL_INVARIANT_IDS)
        if unknown:
            raise ValueError(f"UNKNOWN_INVARIANT_IDS:{unknown}")
        return self


class ScenarioAction(StrictModel):
    action: str = Field(min_length=1, max_length=160)
    parameters: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_sanitized_parameters(self) -> ScenarioAction:
        sanitize_metadata(self.parameters)
        return self


class ScenarioAssertion(StrictModel):
    assertion_id: str = Field(min_length=1, max_length=160)
    assertion_type: str = Field(min_length=1, max_length=80)
    expected_property: str = Field(min_length=1, max_length=500)
    evaluator: str = Field(min_length=1, max_length=160)
    blocking: bool = True
    evidence: tuple[str, ...] = ()


class ScenarioExecution(StrictModel):
    setup: tuple[ScenarioAction, ...] = ()
    stimulus: tuple[ScenarioAction, ...] = ()
    waits: tuple[ScenarioAction, ...] = ()
    assertions: tuple[ScenarioAssertion, ...] = ()
    invariants: tuple[str, ...] = ()
    cleanup: tuple[ScenarioAction, ...] = ()

    @model_validator(mode="after")
    def validate_invariants(self) -> ScenarioExecution:
        unknown = sorted(set(self.invariants) - CANONICAL_INVARIANT_IDS)
        if unknown:
            raise ValueError(f"UNKNOWN_EXECUTION_INVARIANTS:{unknown}")
        return self


class EffectBudgetContract(StrictModel):
    effect_types: tuple[str, ...] = ()
    target_scope: Mapping[str, Any] = Field(default_factory=dict)
    max_stimulus: int = Field(default=0, ge=0)
    max_system_response: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def validate_sanitized_scope(self) -> EffectBudgetContract:
        sanitize_metadata(self.target_scope)
        return self


class ExecutionLeaseContract(StrictModel):
    required: bool = False
    lease_type: str | None = None
    scope_key: str | None = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> ExecutionLeaseContract:
        if self.required and (not self.lease_type or not self.scope_key):
            raise ValueError("REQUIRED_EXECUTION_LEASE_NEEDS_TYPE_AND_SCOPE")
        return self


class TimeoutPolicy(StrictModel):
    scenario_seconds: int = Field(gt=0)
    step_seconds: int = Field(gt=0)


class RetryPolicy(StrictModel):
    transient_max_retries: int = Field(default=0, ge=0)
    external_effect_blind_retries: int = Field(default=0, ge=0)
    same_correlation_required: bool = True

    @model_validator(mode="after")
    def forbid_blind_external_retry(self) -> RetryPolicy:
        if self.external_effect_blind_retries != 0:
            raise ValueError("EXTERNAL_EFFECT_BLIND_RETRIES_MUST_EQUAL_ZERO")
        if not self.same_correlation_required:
            raise ValueError("RETRY_MUST_PRESERVE_CORRELATION")
        return self


class ScenarioSafety(StrictModel):
    effect_budget: EffectBudgetContract
    execution_lease: ExecutionLeaseContract
    timeout: TimeoutPolicy
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    allowed_target_scope: Mapping[str, Any] = Field(default_factory=dict)
    abort_on_blocking_invariant: bool = True

    @model_validator(mode="after")
    def validate_sanitized_target_scope(self) -> ScenarioSafety:
        sanitize_metadata(self.allowed_target_scope)
        return self


class ScenarioEvidenceContract(StrictModel):
    required: tuple[str, ...] = Field(min_length=1)
    provenance: tuple[str, ...] = Field(min_length=1)


class ScenarioManifest(StrictModel):
    scenario: ScenarioIdentity
    traceability: ScenarioTraceability
    preconditions: tuple[str, ...] = ()
    readiness_requirements: tuple[str, ...] = ()
    execution: ScenarioExecution
    safety: ScenarioSafety
    evidence: ScenarioEvidenceContract
    expected_finding_behavior: str = "NONE"
    safe_execution_stage: str

    @model_validator(mode="after")
    def validate_safety_contract(self) -> ScenarioManifest:
        identity = self.scenario
        budget = self.safety.effect_budget
        external = identity.effect_class in {
            EffectClass.SYNTHETIC_EXTERNAL_EFFECT,
            EffectClass.HUMAN_CANARY_EXTERNAL_EFFECT,
        }
        if external:
            if not self.safety.execution_lease.required:
                raise ValueError("EXTERNAL_EFFECT_REQUIRES_EXECUTION_LEASE")
            if not budget.target_scope or not self.safety.allowed_target_scope:
                raise ValueError("EXTERNAL_EFFECT_REQUIRES_BOUNDED_TARGET_SCOPE")
            if dict(budget.target_scope) != dict(self.safety.allowed_target_scope):
                raise ValueError("BUDGET_AND_ALLOWED_TARGET_SCOPE_MUST_MATCH")
        else:
            if budget.max_stimulus or budget.max_system_response:
                raise ValueError("NON_EXTERNAL_SCENARIO_BUDGET_MUST_EQUAL_ZERO")
            if self.safety.allowed_target_scope:
                raise ValueError("NON_EXTERNAL_SCENARIO_TARGET_SCOPE_MUST_BE_EMPTY")

        if identity.effect_class is EffectClass.SYNTHETIC_EXTERNAL_EFFECT:
            if identity.level is not ScenarioTestLevel.L1_SYNTHETIC_E2E:
                raise ValueError("SYNTHETIC_EXTERNAL_EFFECT_REQUIRES_L1")
            if identity.executor_kind is not ExecutorKind.SYNTHETIC_DRIVER:
                raise ValueError("SYNTHETIC_EXTERNAL_EFFECT_REQUIRES_SYNTHETIC_DRIVER")
            if budget.max_stimulus > 1 or budget.max_system_response > 1:
                raise ValueError("SYNTHETIC_EXTERNAL_V1_LIMIT_IS_ONE_PLUS_ONE")
            if budget.max_stimulus + budget.max_system_response == 0:
                raise ValueError("SYNTHETIC_EXTERNAL_EFFECT_REQUIRES_NONZERO_BUDGET")
        if identity.level is ScenarioTestLevel.L0_UNIT_DRY and external:
            raise ValueError("L0_CANNOT_DECLARE_EXTERNAL_EFFECT")
        if set(self.execution.invariants) - set(self.traceability.invariant_ids):
            raise ValueError("EXECUTION_INVARIANT_MISSING_FROM_TRACEABILITY")
        return self

    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_manifest_text(document: str) -> ScenarioManifest:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment contract
        raise ScenarioContractError("PYYAML_REQUIRED_FOR_SCENARIO_MANIFEST") from exc
    try:
        data = yaml.safe_load(document)
    except yaml.YAMLError as exc:
        raise ScenarioContractError("SCENARIO_MANIFEST_YAML_INVALID") from exc
    if not isinstance(data, dict):
        raise ScenarioContractError("SCENARIO_MANIFEST_ROOT_MUST_BE_MAPPING")
    return ScenarioManifest.model_validate(data)


def load_manifest_file(path: Path) -> ScenarioManifest:
    return load_manifest_text(path.read_text(encoding="utf-8"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}_{digest}"


def register_manifest_version(
    session: Session,
    *,
    tenant_id: str,
    manifest: ScenarioManifest,
    manifest_source_path: str,
    source_sha: str,
    now: datetime | None = None,
) -> ScenarioVersionRow:
    """Register immutable manifest metadata without copying the YAML payload."""

    timestamp = now or datetime.now(UTC)
    identity = manifest.scenario
    definition = session.execute(
        select(ScenarioDefinitionRow).where(
            ScenarioDefinitionRow.tenant_id == tenant_id,
            ScenarioDefinitionRow.scenario_key == identity.id,
        )
    ).scalar_one_or_none()
    if definition is None:
        definition = ScenarioDefinitionRow(
            id=_stable_id("scndef", tenant_id, identity.id),
            tenant_id=tenant_id,
            scenario_key=identity.id,
            title=identity.title,
            description=identity.title,
            enabled=True,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(definition)
        session.flush()
    existing = session.execute(
        select(ScenarioVersionRow).where(
            ScenarioVersionRow.scenario_definition_id == definition.id,
            ScenarioVersionRow.version == identity.version,
        )
    ).scalar_one_or_none()
    content_hash = manifest.content_hash()
    if existing is not None:
        if existing.content_hash != content_hash or existing.source_sha != source_sha:
            raise ScenarioContractError("IMMUTABLE_SCENARIO_VERSION_MISMATCH")
        return existing
    row = ScenarioVersionRow(
        id=_stable_id("scnver", tenant_id, identity.id, str(identity.version), content_hash),
        tenant_id=tenant_id,
        scenario_definition_id=definition.id,
        version=identity.version,
        schema_version="1",
        manifest_source_path=manifest_source_path,
        content_hash=content_hash,
        requirements_covered=list(manifest.traceability.pe_ids),
        risk_ids=list(manifest.traceability.risk_ids),
        config_keys=list(manifest.traceability.config_keys),
        risk_classification=identity.priority.value,
        enabled=True,
        source_sha=source_sha,
        created_at=timestamp,
        is_immutable=True,
    )
    session.add(row)
    session.flush()
    audit(
        session, None, "scenario_version_registered",
        {"scenario_definition_id": definition.id, "scenario_version_id": row.id,
         "scenario_key": identity.id, "version": identity.version},
        tenant_id=tenant_id,
    )
    return row


def create_scenario_run(
    session: Session,
    *,
    run_id: str,
    tenant_id: str,
    scenario_version_id: str,
    synthetic_actor_binding_id: str | None,
    agent_execution_intent_id: str | None = None,
    root_correlation_id: str,
    source_sha: str,
    runtime_sha: str,
    schema_revision: str,
    driver_revision: str | None,
    readiness_result_id: str | None,
    effect_budget_id: str | None,
    expires_at: datetime,
    require_synthetic_actor: bool = False,
    now: datetime | None = None,
) -> ScenarioRunRow:
    timestamp = now or datetime.now(UTC)
    version = session.get(ScenarioVersionRow, scenario_version_id)
    if version is None or version.tenant_id != tenant_id:
        raise ScenarioContractError("SCENARIO_VERSION_MISSING_OR_TENANT_MISMATCH")
    if not version.enabled or not version.is_immutable:
        raise ScenarioContractError("SCENARIO_VERSION_NOT_EXECUTABLE")
    if expires_at <= timestamp:
        raise ScenarioContractError("SCENARIO_EXPIRY_MUST_BE_FUTURE")
    if require_synthetic_actor and not synthetic_actor_binding_id:
        raise ScenarioContractError("SYNTHETIC_ACTOR_BINDING_REQUIRED")
    if agent_execution_intent_id and synthetic_actor_binding_id:
        raise ScenarioContractError("PRODUCTION_RUN_CANNOT_USE_SYNTHETIC_ACTOR")
    if synthetic_actor_binding_id:
        actor = session.get(ActorBindingRow, synthetic_actor_binding_id)
        if actor is None or actor.tenant_id != tenant_id:
            raise ScenarioContractError("SCENARIO_ACTOR_MISSING_OR_TENANT_MISMATCH")
        if not actor.is_active:
            raise ScenarioContractError("SCENARIO_ACTOR_NOT_STRUCTURALLY_SYNTHETIC")
        try:
            require_structurally_synthetic_binding(actor)
        except LineageDenied as exc:
            raise ScenarioContractError(
                "SCENARIO_ACTOR_NOT_STRUCTURALLY_SYNTHETIC"
            ) from exc
    row = ScenarioRunRow(
        id=run_id,
        tenant_id=tenant_id,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=synthetic_actor_binding_id,
        agent_execution_intent_id=agent_execution_intent_id,
        status=ScenarioRunStatus.CREATED.value,
        root_correlation_id=root_correlation_id,
        source_sha=source_sha,
        runtime_sha=runtime_sha,
        schema_revision=schema_revision,
        driver_revision=driver_revision,
        readiness_result_id=readiness_result_id,
        effect_budget_id=effect_budget_id,
        created_at=timestamp,
        validated_at=None,
        armed_at=None,
        started_at=None,
        verifying_at=None,
        completed_at=None,
        expires_at=expires_at,
        terminal_reason=None,
        cleanup_state="PENDING",
        updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    return row


_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"VALIDATING", "ABORTED", "EXPIRED"}),
    "VALIDATING": frozenset({"NOT_READY", "ARMED", "FAILED", "ABORTED", "EXPIRED"}),
    "NOT_READY": frozenset({"VALIDATING", "ABORTED", "EXPIRED"}),
    "ARMED": frozenset({"RUNNING", "ABORTED", "EXPIRED"}),
    "RUNNING": frozenset({"VERIFYING", "FAILED", "ABORTED", "EXPIRED"}),
    "VERIFYING": frozenset({"PASSED", "FAILED", "ABORTED", "EXPIRED"}),
}


def transition_scenario_run(
    run: ScenarioRunRow,
    target: ScenarioRunStatus,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(UTC)
    current = run.status
    if current in TERMINAL_RUN_STATUSES:
        raise ScenarioContractError(f"TERMINAL_SCENARIO_{current}_IS_IMMUTABLE")
    if target.value not in _RUN_TRANSITIONS.get(current, frozenset()):
        raise ScenarioContractError(f"INVALID_SCENARIO_TRANSITION:{current}->{target.value}")
    run.status = target.value
    run.updated_at = timestamp
    if target is ScenarioRunStatus.VALIDATING:
        run.validated_at = timestamp
    elif target is ScenarioRunStatus.ARMED:
        run.armed_at = timestamp
    elif target is ScenarioRunStatus.RUNNING:
        run.started_at = timestamp
    elif target is ScenarioRunStatus.VERIFYING:
        run.verifying_at = timestamp
    if target.value in TERMINAL_RUN_STATUSES:
        run.completed_at = timestamp
        run.terminal_reason = reason or target.value


def require_scenario_effect_allowed(run: ScenarioRunRow, *, now: datetime | None = None) -> None:
    timestamp = now or datetime.now(UTC)
    if run.status in TERMINAL_RUN_STATUSES or run.status == ScenarioRunStatus.NOT_READY.value:
        raise ScenarioContractError(f"SCENARIO_STATUS_{run.status}_DENIES_EFFECT")
    if run.status not in {ScenarioRunStatus.ARMED.value, ScenarioRunStatus.RUNNING.value}:
        raise ScenarioContractError(f"SCENARIO_STATUS_{run.status}_DENIES_EFFECT")
    expiry = run.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if timestamp >= expiry:
        raise ScenarioContractError("SCENARIO_EXPIRED_DENIES_EFFECT")


def create_or_resume_step(
    session: Session,
    *,
    run: ScenarioRunRow,
    step_key: str,
    step_order: int,
    step_kind: str,
    idempotency_key: str,
    correlation_id: str,
    max_attempts: int,
    now: datetime | None = None,
) -> tuple[ScenarioStepRunRow, bool]:
    """Return a durable step; external stimulus steps are never recreated."""

    require_scenario_effect_allowed(run, now=now) if step_kind == "STIMULUS" else None
    existing = session.execute(
        select(ScenarioStepRunRow).where(
            ScenarioStepRunRow.scenario_run_id == run.id,
            ScenarioStepRunRow.step_key == step_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.idempotency_key != idempotency_key:
            raise ScenarioContractError("SCENARIO_STEP_IDEMPOTENCY_MISMATCH")
        if existing.step_kind == "STIMULUS" and existing.status in {
            ScenarioStepStatus.RUNNING.value,
            ScenarioStepStatus.VERIFYING.value,
            ScenarioStepStatus.RECONCILIATION_REQUIRED.value,
            ScenarioStepStatus.PASSED.value,
        }:
            return existing, True
        if existing.status in TERMINAL_STEP_STATUSES:
            return existing, True
        return existing, True
    if run.status in TERMINAL_RUN_STATUSES:
        raise ScenarioContractError("TERMINAL_SCENARIO_CANNOT_CREATE_STEP")
    timestamp = now or datetime.now(UTC)
    row = ScenarioStepRunRow(
        id=_stable_id("scnstep", run.id, step_key),
        tenant_id=run.tenant_id,
        scenario_run_id=run.id,
        step_key=step_key,
        step_order=step_order,
        step_kind=step_kind,
        status=ScenarioStepStatus.PENDING.value,
        attempt=0,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        execution_lease_id=None,
        created_at=timestamp,
        started_at=None,
        completed_at=None,
        updated_at=timestamp,
        failure_reason=None,
        cleanup_state="PENDING",
    )
    session.add(row)
    session.flush()
    return row, False


_STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"RUNNING", "SKIPPED", "ABORTED", "EXPIRED"}),
    "RUNNING": frozenset(
        {"VERIFYING", "PASSED", "FAILED", "ABORTED", "EXPIRED", "RECONCILIATION_REQUIRED"}
    ),
    "VERIFYING": frozenset({"PASSED", "FAILED", "ABORTED", "EXPIRED"}),
    "RECONCILIATION_REQUIRED": frozenset({"VERIFYING", "FAILED", "ABORTED", "EXPIRED"}),
}


def transition_scenario_step(
    step: ScenarioStepRunRow,
    target: ScenarioStepStatus,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(UTC)
    current = step.status
    if current in TERMINAL_STEP_STATUSES:
        raise ScenarioContractError(f"TERMINAL_SCENARIO_STEP_{current}_IS_IMMUTABLE")
    if target.value not in _STEP_TRANSITIONS.get(current, frozenset()):
        raise ScenarioContractError(f"INVALID_SCENARIO_STEP_TRANSITION:{current}->{target.value}")
    if target is ScenarioStepStatus.RUNNING:
        if step.attempt >= step.max_attempts:
            raise ScenarioContractError("SCENARIO_STEP_MAX_ATTEMPTS_EXHAUSTED")
        step.attempt += 1
        step.started_at = step.started_at or timestamp
    step.status = target.value
    step.updated_at = timestamp
    if target.value in TERMINAL_STEP_STATUSES:
        step.completed_at = timestamp
        step.failure_reason = reason if target is not ScenarioStepStatus.PASSED else None
    elif target is ScenarioStepStatus.RECONCILIATION_REQUIRED:
        step.failure_reason = reason or "EXTERNAL_EFFECT_STATE_UNCERTAIN"


@dataclass(frozen=True, slots=True)
class ScenarioResumeDecision:
    disposition: ResumeDisposition
    scenario_run_id: str
    step_id: str | None
    reason_code: str


def derive_resume_decision(
    session: Session,
    *,
    scenario_run_id: str,
    now: datetime | None = None,
) -> ScenarioResumeDecision:
    """Read durable state and never infer a new stimulus after restart."""

    timestamp = now or datetime.now(UTC)
    run = session.execute(
        select(ScenarioRunRow)
        .where(ScenarioRunRow.id == scenario_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise ScenarioContractError("SCENARIO_RUN_MISSING_ON_RESUME")
    if run.status in TERMINAL_RUN_STATUSES:
        return ScenarioResumeDecision(
            ResumeDisposition.TERMINAL,
            run.id,
            None,
            f"TERMINAL_SCENARIO_{run.status}",
        )
    expiry = run.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if timestamp >= expiry:
        transition_scenario_run(
            run,
            ScenarioRunStatus.EXPIRED,
            reason="SCENARIO_EXPIRED_DURING_RECOVERY",
            now=timestamp,
        )
        session.flush()
        return ScenarioResumeDecision(
            ResumeDisposition.EXPIRED,
            run.id,
            None,
            "SCENARIO_EXPIRED_DURING_RECOVERY",
        )
    steps = session.scalars(
        select(ScenarioStepRunRow)
        .where(
            ScenarioStepRunRow.scenario_run_id == run.id,
            ScenarioStepRunRow.status.not_in(TERMINAL_STEP_STATUSES),
        )
        .order_by(ScenarioStepRunRow.step_order)
        .with_for_update()
    ).all()
    if not steps:
        return ScenarioResumeDecision(
            ResumeDisposition.NO_ACTIVE_STEP,
            run.id,
            None,
            "NO_ACTIVE_STEP",
        )
    step = steps[0]
    if step.step_kind == "STIMULUS" and step.status in {
        ScenarioStepStatus.RUNNING.value,
        ScenarioStepStatus.VERIFYING.value,
        ScenarioStepStatus.RECONCILIATION_REQUIRED.value,
    }:
        if step.status != ScenarioStepStatus.RECONCILIATION_REQUIRED.value:
            transition_scenario_step(
                step,
                ScenarioStepStatus.RECONCILIATION_REQUIRED,
                reason="STIMULUS_OUTCOME_REQUIRES_RECONCILIATION",
                now=timestamp,
            )
            session.flush()
        return ScenarioResumeDecision(
            ResumeDisposition.RECONCILIATION_REQUIRED,
            run.id,
            step.id,
            "STIMULUS_MUST_NOT_BE_REPEATED",
        )
    return ScenarioResumeDecision(
        ResumeDisposition.RESUME_EXISTING_STEP,
        run.id,
        step.id,
        "RESUME_DURABLE_STEP",
    )


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    run_id: str
    status: ScenarioRunStatus
    blocking_failures: tuple[str, ...]
    evidence_complete: bool


def derive_terminal_evaluation(
    *,
    run_id: str,
    blocking_assertion_failures: Iterable[str],
    blocking_invariant_failures: Iterable[str],
    evidence_complete: bool,
) -> ScenarioEvaluation:
    failures = tuple(blocking_assertion_failures) + tuple(blocking_invariant_failures)
    if failures:
        status = ScenarioRunStatus.FAILED
    elif not evidence_complete:
        status = ScenarioRunStatus.ABORTED
    else:
        status = ScenarioRunStatus.PASSED
    return ScenarioEvaluation(
        run_id=run_id,
        status=status,
        blocking_failures=failures,
        evidence_complete=evidence_complete,
    )
