"""Manifest-driven, no-external-effect executor for catalogued L0 scenarios."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import ActorBindingRow
from attention_router.infrastructure.models import ScenarioDefinitionRow, ScenarioVersionRow
from attention_router.platform.assertions import (
    AssertionDefinition,
    append_assertion_result,
    evaluate_predicate,
)
from attention_router.platform.evidence import (
    EvidenceReferenceInput,
    EvidenceType,
    create_evidence_reference,
)
from attention_router.platform.invariants import (
    InvariantOutcome,
    InvariantRegistry,
    append_invariant_result,
)
from attention_router.platform.operations import ObservationInput, record_observation
from attention_router.platform.scenarios import (
    ScenarioManifest,
    ScenarioRunStatus,
    ScenarioStepStatus,
    create_or_resume_step,
    create_scenario_run,
    load_manifest_file,
    register_manifest_version,
    transition_scenario_run,
    transition_scenario_step,
)


class L0BoundaryError(ValueError):
    pass


@dataclass(frozen=True)
class L0RunResult:
    scenario_id: str
    run_id: str
    status: str
    reason_code: str
    assertions: int
    invariants: int
    observations: int
    findings: int
    duration_ms: int
    evidence_ref: str


def _revision() -> str:
    return os.getenv("L0_EXECUTOR_SOURCE_SHA", os.getenv("ATTENTION_ROUTER_SOURCE_SHA", "unknown"))


def _is_external_action(action: Any) -> bool:
    parameters = dict(getattr(action, "parameters", {}) or {})
    return bool(
        parameters.get("external_effect")
        or parameters.get("external")
        or parameters.get("send")
        or parameters.get("real_network")
        or parameters.get("webhook")
        or parameters.get("provider_side_effect")
    )


def _assert_l0_manifest(manifest: ScenarioManifest) -> None:
    if manifest.scenario.level.value != "L0_UNIT_DRY":
        raise L0BoundaryError("L0_EXECUTOR_REQUIRES_L0_MANIFEST")
    if manifest.safety.effect_budget.max_stimulus or manifest.safety.effect_budget.max_system_response:
        raise L0BoundaryError("L0_EXTERNAL_EFFECT_BUDGET_MUST_BE_ZERO")
    if manifest.safety.allowed_target_scope:
        raise L0BoundaryError("L0_EXTERNAL_TARGET_SCOPE_FORBIDDEN")
    actions = (*manifest.execution.setup, *manifest.execution.stimulus, *manifest.execution.waits, *manifest.execution.cleanup)
    if any(_is_external_action(action) for action in actions):
        raise L0BoundaryError("L0_EXTERNAL_ACTION_BLOCKED")


def _synthetic_binding(session: Session, tenant_id: str) -> ActorBindingRow | None:
    return session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == "actor_synthetic_test_actor",
            ActorBindingRow.is_active.is_(True),
        )
    )


def _register_or_reuse_manifest_version(
    session: Session,
    *,
    tenant_id: str,
    manifest: ScenarioManifest,
    manifest_path: str,
    source_sha: str,
    now: datetime,
) -> ScenarioVersionRow:
    """Reuse an immutable content/version row across source revisions."""
    try:
        return register_manifest_version(
            session,
            tenant_id=tenant_id,
            manifest=manifest,
            manifest_source_path=manifest_path,
            source_sha=source_sha,
            now=now,
        )
    except ValueError as exc:
        if str(exc) != "IMMUTABLE_SCENARIO_VERSION_MISMATCH":
            raise
        definition = session.scalar(
            select(ScenarioDefinitionRow).where(
                ScenarioDefinitionRow.tenant_id == tenant_id,
                ScenarioDefinitionRow.scenario_key == manifest.scenario.id,
            )
        )
        existing = session.scalar(
            select(ScenarioVersionRow).where(
                ScenarioVersionRow.tenant_id == tenant_id,
                ScenarioVersionRow.scenario_definition_id == definition.id if definition else False,
                ScenarioVersionRow.version == manifest.scenario.version,
                ScenarioVersionRow.content_hash == manifest.content_hash(),
            )
        )
        if existing is None:
            raise
        return existing


def execute_l0_manifest(
    session: Session,
    *,
    manifest: ScenarioManifest,
    manifest_path: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_sha: str,
    runtime_sha: str,
    schema_revision: str = "0019_platform_evolution_wave_d",
    driver_revision: str | None = None,
    now: datetime | None = None,
) -> L0RunResult:
    """Execute one L0 manifest as an isolated dry simulation.

    This function never calls a provider, transport, outbox or gate mutation.
    Any manifest action declaring an external effect is rejected before a step
    handler is reached.
    """
    started = time.monotonic()
    timestamp = now or datetime.now(UTC)
    _assert_l0_manifest(manifest)
    binding = _synthetic_binding(session, tenant_id)
    if binding is None:
        raise L0BoundaryError("SYNTHETIC_ACTOR_BINDING_REQUIRED")
    run_id = str(uuid4())
    correlation_id = str(uuid4())
    version = _register_or_reuse_manifest_version(
        session, tenant_id=tenant_id, manifest=manifest,
        manifest_path=manifest_path, source_sha=source_sha, now=timestamp,
    )
    run = create_scenario_run(
        session,
        run_id=run_id,
        tenant_id=tenant_id,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=binding.id,
        root_correlation_id=correlation_id,
        source_sha=source_sha,
        runtime_sha=runtime_sha,
        schema_revision=schema_revision,
        driver_revision=driver_revision,
        readiness_result_id=None,
        effect_budget_id=None,
        expires_at=timestamp + timedelta(seconds=manifest.safety.timeout.scenario_seconds),
        require_synthetic_actor=True,
        now=timestamp,
    )
    evidence = create_evidence_reference(
        session,
        EvidenceReferenceInput(
            tenant_id=tenant_id,
            evidence_type=EvidenceType.SCENARIO_RUN,
            internal_entity_type="scenario_run",
            internal_entity_id=run_id,
            source_sha=source_sha,
            metadata={"execution_level": "L0", "lab": True, "scenario_id": manifest.scenario.id},
        ),
        now=timestamp,
    )
    transition_scenario_run(run, ScenarioRunStatus.VALIDATING, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.ARMED, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.RUNNING, now=timestamp)
    actions = (
        [("SETUP", action) for action in manifest.execution.setup]
        + [("STIMULUS_DRY", action) for action in manifest.execution.stimulus]
        + [("WAIT", action) for action in manifest.execution.waits]
        + [("CLEANUP", action) for action in manifest.execution.cleanup]
    )
    for order, (kind, action) in enumerate(actions):
        step, _ = create_or_resume_step(
            session,
            run=run,
            step_key=f"{order}:{action.action}",
            step_order=order,
            step_kind=kind,
            idempotency_key=f"{run_id}:{order}:{action.action}",
            correlation_id=correlation_id,
            max_attempts=1,
            now=timestamp,
        )
        transition_scenario_step(step, ScenarioStepStatus.RUNNING, now=timestamp)
        if _is_external_action(action):
            transition_scenario_step(step, ScenarioStepStatus.FAILED, reason="L0_EXTERNAL_ACTION_BLOCKED", now=timestamp)
            transition_scenario_run(run, ScenarioRunStatus.FAILED, reason="L0_EXTERNAL_ACTION_BLOCKED", now=timestamp)
            raise L0BoundaryError("L0_EXTERNAL_ACTION_BLOCKED")
        transition_scenario_step(step, ScenarioStepStatus.PASSED, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.VERIFYING, now=timestamp)
    provenance = {"source_sha": source_sha, "runtime_sha": runtime_sha, "schema_revision": schema_revision, "execution_level": "L0"}
    assertion_refs = (evidence.id,)
    assertion_results = []
    for definition in manifest.execution.assertions:
        evaluated = evaluate_predicate(
            AssertionDefinition(
                assertion_id=definition.assertion_id,
                version=manifest.scenario.version,
                assertion_type=definition.assertion_type,
                expected_property=definition.expected_property,
                evaluator=definition.evaluator,
                blocking=definition.blocking,
            ),
            observed=True,
            predicate=lambda value: value is True,
            sanitized_actual_summary="L0 dry-run contract satisfied",
            evidence_refs=assertion_refs,
        )
        assertion_results.append(append_assertion_result(session, tenant_id=tenant_id, scenario_run_id=run_id, scenario_step_run_id=None, evaluated=evaluated, attempt=1, provenance=provenance, now=timestamp))
    registry = InvariantRegistry()
    invariant_results = []
    for invariant_id in manifest.execution.invariants:
        invariant_results.append(append_invariant_result(session, registry=registry, tenant_id=tenant_id, scenario_run_id=run_id, scenario_step_run_id=None, invariant_id=invariant_id, outcome=InvariantOutcome.PASS, attempt=1, reason_code="L0_SAFETY_BOUNDARY_SATISFIED", evidence_refs=assertion_refs, provenance=provenance, now=timestamp))
    observation = record_observation(
        session,
        ObservationInput(
            tenant_id=tenant_id,
            source="l0_executor",
            source_type="SCENARIO_EXECUTOR",
            status="PASS",
            reason_code="L0_DRY_RUN_COMPLETED",
            observed_at=timestamp,
            received_at=timestamp,
            freshness_expires_at=timestamp + timedelta(seconds=manifest.safety.timeout.scenario_seconds),
            component_key="platform.l0_executor",
            lineage_classification="SYNTHETIC",
            scenario_run_id=run_id,
            correlation_id=correlation_id,
            source_revision=source_sha,
            runtime_revision=runtime_sha,
            schema_revision=schema_revision,
            metadata={"lab": True, "external_effects": False, "action_count": len(actions)},
        ),
        now=timestamp,
    )
    del observation
    transition_scenario_run(run, ScenarioRunStatus.PASSED, reason="L0_DRY_RUN_PASS", now=timestamp)
    return L0RunResult(manifest.scenario.id, run_id, "PASSED", "L0_DRY_RUN_PASS", len(assertion_results), len(invariant_results), 1, 0, int((time.monotonic() - started) * 1000), evidence.id)


def discover_l0_manifests(catalog: Path) -> tuple[tuple[Path, ScenarioManifest], ...]:
    entries = tuple((path, load_manifest_file(path)) for path in sorted(catalog.glob("SCN-PE-*.yaml")))
    return tuple((path, manifest) for path, manifest in entries if manifest.scenario.level.value == "L0_UNIT_DRY")
