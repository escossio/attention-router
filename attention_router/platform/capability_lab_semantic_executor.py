"""Scenario Engine executor for real Capability Lab T0 semantics.

Unlike the generic L0 dry-run executor, this boundary does not manufacture an
`observed=True` assertion. It delegates to the canonical capability resolver,
persists the existing T0 evidence contract, and lets Scenario Engine status follow
that semantic result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.authority import AuthorityResult
from attention_router.core.capability_lab import CapabilityLabScenario, ComparisonStatus
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ScenarioDefinitionRow,
    ScenarioVersionRow,
)
from attention_router.platform.assertions import (
    AssertionDefinition,
    AssertionOutcome,
    append_assertion_result,
    evaluate_predicate,
)
from attention_router.platform.capability_lab_probe import record_capability_t0_evidence
from attention_router.platform.evidence import (
    EvidenceReferenceInput,
    EvidenceType,
    create_evidence_reference,
)
from attention_router.platform.scenarios import (
    EffectClass,
    ExecutorKind,
    ScenarioManifest,
    ScenarioRunStatus,
    ScenarioStepStatus,
    ScenarioTestLevel,
    create_or_resume_step,
    create_scenario_run,
    derive_terminal_evaluation,
    register_manifest_version,
    transition_scenario_run,
    transition_scenario_step,
)


_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,159}$")
_EXTERNAL_MARKERS = (
    "external_effect",
    "external",
    "send",
    "real_network",
    "webhook",
    "provider_side_effect",
)


class CapabilityLabSemanticBoundaryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityLabSemanticRunResult:
    scenario_key: str
    run_id: str
    status: str
    reason_code: str
    assertion_result_id: str
    scenario_evidence_ref: str
    t0_evidence_ref: str


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
    """Reuse immutable manifest content when only the surrounding source SHA moved."""

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
                ScenarioVersionRow.scenario_definition_id == (
                    definition.id if definition is not None else ""
                ),
                ScenarioVersionRow.version == manifest.scenario.version,
                ScenarioVersionRow.content_hash == manifest.content_hash(),
            )
        )
        if existing is None:
            raise
        return existing


def _declares_external_effect(manifest: ScenarioManifest) -> bool:
    actions = (
        *manifest.execution.setup,
        *manifest.execution.stimulus,
        *manifest.execution.waits,
        *manifest.execution.cleanup,
    )
    return any(
        any(bool(dict(action.parameters).get(marker)) for marker in _EXTERNAL_MARKERS)
        for action in actions
    )


def _semantic_contract(manifest: ScenarioManifest) -> tuple[CapabilityLabScenario, str]:
    identity = manifest.scenario
    execution = manifest.execution

    if identity.level is not ScenarioTestLevel.L0_UNIT_DRY:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_REQUIRES_L0")
    if identity.executor_kind is not ExecutorKind.SCENARIO_ENGINE:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_REQUIRES_SCENARIO_ENGINE")
    if identity.effect_class is not EffectClass.NO_EXTERNAL_EFFECT:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_EXTERNAL_EFFECT_FORBIDDEN")
    if (
        manifest.safety.effect_budget.max_stimulus
        or manifest.safety.effect_budget.max_system_response
        or manifest.safety.allowed_target_scope
        or _declares_external_effect(manifest)
    ):
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_EFFECT_BUDGET_MUST_BE_ZERO")
    if execution.setup or execution.waits or execution.cleanup:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_V0_EXTRA_ACTIONS_FORBIDDEN")
    if len(execution.stimulus) != 1:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_ONE_STIMULUS_REQUIRED")
    if execution.invariants:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_V0_INVARIANTS_UNSUPPORTED")
    if len(execution.assertions) != 1:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_ONE_ASSERTION_REQUIRED")

    action = execution.stimulus[0]
    if action.action != "capability_lab_t0_resolve":
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_ACTION_UNSUPPORTED")
    assertion = execution.assertions[0]
    if assertion.evaluator != "capability_lab_t0_semantic_evaluator":
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_EVALUATOR_REQUIRED")

    params = dict(action.parameters)
    required = {
        "lab_scenario_id",
        "capability",
        "requester_actor_key",
        "expected_resolution",
        "expected_reason_code",
    }
    if set(params) != required:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_PARAMETERS_INVALID")

    expected_reason = str(params["expected_reason_code"]).strip()
    if not _REASON_CODE_RE.fullmatch(expected_reason):
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_REASON_CODE_INVALID")
    try:
        expected_resolution = AuthorityResult(str(params["expected_resolution"]).strip())
    except ValueError as exc:
        raise CapabilityLabSemanticBoundaryError(
            "CAPABILITY_LAB_SEMANTIC_EXPECTED_RESOLUTION_INVALID"
        ) from exc

    scenario = CapabilityLabScenario(
        scenario_id=str(params["lab_scenario_id"]),
        title=identity.title,
        description="Scenario Engine semantic T0 control over the canonical capability resolver.",
        capability_key=str(params["capability"]),
        requester_actor_key=str(params["requester_actor_key"]),
        request_text="Synthetic Capability Lab T0 control request",
        expected_resolution=expected_resolution,
        engine_scenario_key=identity.id,
    )
    required_evidence = set(manifest.evidence.required)
    if not {"SCENARIO_RUN", "API_RESULT", "ASSERTION_RESULT"}.issubset(required_evidence):
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_EVIDENCE_CONTRACT_INCOMPLETE")
    return scenario, expected_reason


def execute_capability_lab_t0_manifest(
    session: Session,
    *,
    manifest: ScenarioManifest,
    manifest_path: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    source_sha: str,
    runtime_sha: str,
    schema_revision: str,
    now: datetime | None = None,
) -> CapabilityLabSemanticRunResult:
    """Run one no-external-effect semantic T0 scenario through Scenario Engine."""

    if not source_sha or not runtime_sha or not schema_revision:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_PROVENANCE_REQUIRED")

    scenario, expected_reason = _semantic_contract(manifest)
    timestamp = now or datetime.now(UTC)
    binding = _synthetic_binding(session, tenant_id)
    if binding is None:
        raise CapabilityLabSemanticBoundaryError("SYNTHETIC_ACTOR_BINDING_REQUIRED")

    version = _register_or_reuse_manifest_version(
        session,
        tenant_id=tenant_id,
        manifest=manifest,
        manifest_path=manifest_path,
        source_sha=source_sha,
        now=timestamp,
    )
    run_id = str(uuid4())
    correlation_id = str(uuid4())
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
        driver_revision="capability-lab-t0-semantic-v1",
        readiness_result_id=None,
        effect_budget_id=None,
        expires_at=timestamp + timedelta(seconds=manifest.safety.timeout.scenario_seconds),
        require_synthetic_actor=True,
        now=timestamp,
    )
    scenario_evidence = create_evidence_reference(
        session,
        EvidenceReferenceInput(
            tenant_id=tenant_id,
            evidence_type=EvidenceType.SCENARIO_RUN,
            internal_entity_type="scenario_run",
            internal_entity_id=run.id,
            source_sha=source_sha,
            metadata={
                "lab": True,
                "semantic_stage": "T0",
                "scenario_key": manifest.scenario.id,
                "external_effects": False,
            },
        ),
        now=timestamp,
    )

    transition_scenario_run(run, ScenarioRunStatus.VALIDATING, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.ARMED, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.RUNNING, now=timestamp)
    step, resumed = create_or_resume_step(
        session,
        run=run,
        step_key="0:capability_lab_t0_resolve",
        step_order=0,
        step_kind="SEMANTIC_T0",
        idempotency_key=f"{run.id}:capability-lab-t0",
        correlation_id=correlation_id,
        max_attempts=1,
        now=timestamp,
    )
    if resumed:
        raise CapabilityLabSemanticBoundaryError("CAPABILITY_LAB_SEMANTIC_UNEXPECTED_RESUME")
    transition_scenario_step(step, ScenarioStepStatus.RUNNING, now=timestamp)

    t0 = record_capability_t0_evidence(
        session,
        scenario,
        tenant_id=tenant_id,
        source_sha=source_sha,
        runtime_sha=runtime_sha,
        schema_revision=schema_revision,
        now=timestamp,
        freshness_seconds=min(manifest.safety.timeout.scenario_seconds, 3600),
    )

    transition_scenario_step(step, ScenarioStepStatus.VERIFYING, now=timestamp)
    transition_scenario_run(run, ScenarioRunStatus.VERIFYING, now=timestamp)

    manifest_assertion = manifest.execution.assertions[0]
    actual = {
        "comparison": t0.comparison.status.value,
        "authority": t0.probe.authority_result.value,
        "reason": t0.probe.reason_code,
    }
    expected = {
        "comparison": ComparisonStatus.PASS.value,
        "authority": scenario.expected_resolution.value,
        "reason": expected_reason,
    }
    evaluated = evaluate_predicate(
        AssertionDefinition(
            assertion_id=manifest_assertion.assertion_id,
            version=manifest.scenario.version,
            assertion_type=manifest_assertion.assertion_type,
            expected_property=manifest_assertion.expected_property,
            evaluator=manifest_assertion.evaluator,
            blocking=manifest_assertion.blocking,
        ),
        observed=actual,
        predicate=lambda observed: observed == expected,
        sanitized_actual_summary=(
            f"comparison={actual['comparison']}; authority={actual['authority']}; "
            f"reason={actual['reason']}"
        ),
        evidence_refs=(scenario_evidence.id, t0.evidence_reference_id),
    )
    assertion_row = append_assertion_result(
        session,
        tenant_id=tenant_id,
        scenario_run_id=run.id,
        scenario_step_run_id=step.id,
        evaluated=evaluated,
        attempt=1,
        provenance={
            "source_sha": source_sha,
            "runtime_sha": runtime_sha,
            "schema_revision": schema_revision,
            "scenario_hash": manifest.content_hash(),
            "semantic_stage": "T0",
        },
        now=timestamp,
    )

    failures = () if evaluated.outcome is AssertionOutcome.PASS else (evaluated.definition.assertion_id,)
    terminal = derive_terminal_evaluation(
        run_id=run.id,
        blocking_assertion_failures=failures,
        blocking_invariant_failures=(),
        evidence_complete=bool(
            scenario_evidence.id and t0.evidence_reference_id and assertion_row.id
        ),
    )
    if terminal.status is ScenarioRunStatus.PASSED:
        transition_scenario_step(step, ScenarioStepStatus.PASSED, now=timestamp)
        reason_code = "CAPABILITY_LAB_T0_SEMANTIC_PASS"
    else:
        transition_scenario_step(step, ScenarioStepStatus.FAILED, now=timestamp)
        reason_code = "CAPABILITY_LAB_T0_SEMANTIC_MISMATCH"
    transition_scenario_run(run, terminal.status, reason=reason_code, now=timestamp)

    return CapabilityLabSemanticRunResult(
        scenario_key=manifest.scenario.id,
        run_id=run.id,
        status=terminal.status.value,
        reason_code=reason_code,
        assertion_result_id=assertion_row.id,
        scenario_evidence_ref=scenario_evidence.id,
        t0_evidence_ref=t0.evidence_reference_id,
    )
