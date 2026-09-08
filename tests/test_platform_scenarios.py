from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import ActorBindingRow
from attention_router.platform.assertions import (
    AssertionDefinition,
    append_assertion_result,
    evaluate_predicate,
)
from attention_router.platform.automated_matrix import (
    AutomatedScenarioMatrix,
    ExternalExecutionAuthorization,
    MatrixItemStatus,
    MatrixStatus,
    ScenarioExecutionResult,
)
from attention_router.platform.scenarios import (
    EffectClass,
    ExecutorKind,
    ScenarioContractError,
    ScenarioManifest,
    ScenarioPriority,
    ScenarioStepStatus,
    ScenarioRunStatus,
    ScenarioTestLevel,
    ResumeDisposition,
    create_or_resume_step,
    create_scenario_run,
    derive_resume_decision,
    derive_terminal_evaluation,
    register_manifest_version,
    require_scenario_effect_allowed,
    transition_scenario_run,
    transition_scenario_step,
)
from attention_router.platform.invariants import (
    InvariantOutcome,
    InvariantRegistry,
    append_invariant_result,
)


def _manifest_data(*, external: bool = False) -> dict:
    effect_class = "SYNTHETIC_EXTERNAL_EFFECT" if external else "NO_EXTERNAL_EFFECT"
    level = "L1_SYNTHETIC_E2E" if external else "L0_UNIT_DRY"
    executor = "SYNTHETIC_DRIVER" if external else "POSTGRES_HARNESS"
    target = {"actor_ref": "synthetic-target"} if external else {}
    return {
        "scenario": {
            "id": "SCN-PE-011" if external else "SCN-PE-002",
            "version": 1,
            "title": "bounded scenario",
            "priority": "P0",
            "level": level,
            "executor_kind": executor,
            "effect_class": effect_class,
        },
        "traceability": {
            "pe_ids": ["PE-009"],
            "acceptance_ids": ["ACC-CORE-LEASE-001"],
            "invariant_ids": ["INV-PE-LEASE-001", "INV-PE-SEND-004"],
            "risk_ids": ["RPE-004"],
            "historical_risk_ids": [],
            "coverage_gap_ids": ["COV-GAP-CORE-LEASE-001"],
            "work_packages": ["WP-PE-03"],
            "config_keys": ["EXECUTION_ONESHOT_LEASE_TTL"],
        },
        "preconditions": ["POSTGRES_READY"],
        "readiness_requirements": ["DOMAIN_READINESS_READY"],
        "execution": {
            "setup": [],
            "stimulus": [],
            "waits": [],
            "assertions": [
                {
                    "assertion_id": "single-winner",
                    "assertion_type": "CONCURRENCY",
                    "expected_property": "one lease winner",
                    "evaluator": "postgres-state",
                    "blocking": True,
                    "evidence": ["DB_STATE"],
                }
            ],
            "invariants": ["INV-PE-LEASE-001", "INV-PE-SEND-004"],
            "cleanup": [],
        },
        "safety": {
            "effect_budget": {
                "effect_types": ["WHATSAPP_TEXT"] if external else [],
                "target_scope": target,
                "max_stimulus": 1 if external else 0,
                "max_system_response": 1 if external else 0,
                "ttl_seconds": 60,
            },
            "execution_lease": {
                "required": external,
                "lease_type": "SCENARIO_EFFECT" if external else None,
                "scope_key": "synthetic-target" if external else None,
            },
            "timeout": {"scenario_seconds": 120, "step_seconds": 30},
            "retry": {
                "transient_max_retries": 1,
                "external_effect_blind_retries": 0,
                "same_correlation_required": True,
            },
            "allowed_target_scope": target,
            "abort_on_blocking_invariant": True,
        },
        "evidence": {
            "required": ["DB_STATE", "INVARIANT_RESULT"],
            "provenance": ["source_sha", "runtime_sha", "schema_revision"],
        },
        "expected_finding_behavior": "NONE",
        "safe_execution_stage": "AFTER_WP03_INTERNAL",
    }


def test_manifest_is_strict_immutable_and_hash_stable():
    first = ScenarioManifest.model_validate(_manifest_data())
    second = ScenarioManifest.model_validate(_manifest_data())
    assert first.content_hash() == second.content_hash()
    assert first.scenario.priority is ScenarioPriority.P0
    assert first.scenario.level is ScenarioTestLevel.L0_UNIT_DRY
    assert first.scenario.executor_kind is ExecutorKind.POSTGRES_HARNESS
    assert first.scenario.effect_class is EffectClass.NO_EXTERNAL_EFFECT


def test_manifest_rejects_blind_retry_and_unbounded_synthetic_budget():
    blind_retry = _manifest_data(external=True)
    blind_retry["safety"]["retry"]["external_effect_blind_retries"] = 1
    with pytest.raises(ValidationError, match="EXTERNAL_EFFECT_BLIND_RETRIES_MUST_EQUAL_ZERO"):
        ScenarioManifest.model_validate(blind_retry)

    unbounded = _manifest_data(external=True)
    unbounded["safety"]["effect_budget"]["max_stimulus"] = 2
    with pytest.raises(ValidationError, match="SYNTHETIC_EXTERNAL_V1_LIMIT_IS_ONE_PLUS_ONE"):
        ScenarioManifest.model_validate(unbounded)


def test_terminal_scenario_cannot_transition_or_emit_effect():
    class Run:
        status = "FAILED"
        expires_at = datetime.now(UTC) + timedelta(minutes=1)

    run = Run()
    with pytest.raises(ScenarioContractError, match="TERMINAL_SCENARIO_FAILED_IS_IMMUTABLE"):
        transition_scenario_run(run, ScenarioRunStatus.RUNNING)
    with pytest.raises(ScenarioContractError, match="SCENARIO_STATUS_FAILED_DENIES_EFFECT"):
        require_scenario_effect_allowed(run)


def test_runner_completion_does_not_pass_without_evidence():
    evaluation = derive_terminal_evaluation(
        run_id="run-1",
        blocking_assertion_failures=(),
        blocking_invariant_failures=(),
        evidence_complete=False,
    )
    assert evaluation.status is ScenarioRunStatus.ABORTED


def test_manifest_registration_is_immutable_and_run_pins_version(session):
    now = datetime.now(UTC)
    manifest = ScenarioManifest.model_validate(_manifest_data())
    version = register_manifest_version(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        manifest=manifest,
        manifest_source_path="config/platform/scenarios.v1.yaml",
        source_sha="source-a",
        now=now,
    )
    same = register_manifest_version(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        manifest=manifest,
        manifest_source_path="config/platform/scenarios.v1.yaml",
        source_sha="source-a",
        now=now,
    )
    assert same.id == version.id
    run = create_scenario_run(
        session,
        run_id="run-scenario-1",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=None,
        root_correlation_id="correlation-scenario-1",
        source_sha="source-a",
        runtime_sha="runtime-a",
        schema_revision="0019",
        driver_revision=None,
        readiness_result_id=None,
        effect_budget_id=None,
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    assert run.scenario_version_id == version.id
    assert run.status == "CREATED"

    assertion = evaluate_predicate(
        AssertionDefinition(
            assertion_id="manifest-pinned",
            version=1,
            assertion_type="PROVENANCE",
            expected_property="run pins exact scenario version",
            evaluator="deterministic-version-check",
        ),
        observed=run.scenario_version_id,
        predicate=lambda observed: observed == version.id,
        sanitized_actual_summary="exact version matched",
        evidence_refs=("ev-version",),
    )
    assertion_row = append_assertion_result(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        evaluated=assertion,
        attempt=1,
        provenance={"source_sha": "source-a"},
        now=now,
    )
    invariant_row = append_invariant_result(
        session,
        registry=InvariantRegistry(),
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        scenario_step_run_id=None,
        invariant_id="INV-PE-SCEN-006",
        outcome=InvariantOutcome.PASS,
        attempt=1,
        reason_code="MANIFEST_VERSION_HASH_MATCHED",
        evidence_refs=("ev-version",),
        provenance={"source_sha": "source-a"},
        now=now,
    )
    assert assertion_row.result == "PASS"
    assert invariant_row.result == "PASS"

    transition_scenario_run(run, ScenarioRunStatus.VALIDATING, now=now)
    transition_scenario_run(run, ScenarioRunStatus.ARMED, now=now)
    transition_scenario_run(run, ScenarioRunStatus.RUNNING, now=now)
    step, resumed = create_or_resume_step(
        session,
        run=run,
        step_key="stimulus",
        step_order=1,
        step_kind="STIMULUS",
        idempotency_key="stimulus-key-1",
        correlation_id=run.root_correlation_id,
        max_attempts=1,
        now=now,
    )
    assert not resumed
    transition_scenario_step(step, ScenarioStepStatus.RUNNING, now=now)
    decision = derive_resume_decision(session, scenario_run_id=run.id, now=now)
    assert decision.disposition is ResumeDisposition.RECONCILIATION_REQUIRED
    same_step, resumed = create_or_resume_step(
        session,
        run=run,
        step_key="stimulus",
        step_order=1,
        step_kind="STIMULUS",
        idempotency_key="stimulus-key-1",
        correlation_id=run.root_correlation_id,
        max_attempts=1,
        now=now,
    )
    assert resumed
    assert same_step.id == step.id
    assert same_step.attempt == 1

    changed = _manifest_data()
    changed["scenario"]["title"] = "changed immutable content"
    with pytest.raises(ScenarioContractError, match="IMMUTABLE_SCENARIO_VERSION_MISMATCH"):
        register_manifest_version(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            manifest=ScenarioManifest.model_validate(changed),
            manifest_source_path="config/platform/scenarios.v1.yaml",
            source_sha="source-a",
            now=now,
        )


def test_automated_matrix_never_self_authorizes_external_scenario():
    manifest = ScenarioManifest.model_validate(_manifest_data(external=True))
    matrix = AutomatedScenarioMatrix([manifest])
    now = datetime.now(UTC)
    planned = matrix.plan([manifest.scenario.id], now=now)
    assert planned[0].blocked_reason == "EXTERNAL_EFFECT_NOT_AUTHORIZED"

    authorization = ExternalExecutionAuthorization(
        scenario_id=manifest.scenario.id,
        scenario_version=manifest.scenario.version,
        scenario_content_hash=manifest.content_hash(),
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id="run-1",
        expires_at=now + timedelta(minutes=5),
        target_scope=manifest.safety.allowed_target_scope,
        driver_revision="driver-source-a",
        driver_session_id="synthetic-session-ref",
        owner_session_id="owner-session-ref",
        requested_by_human=True,
        gate_e1_passed=True,
        lease_reserved=True,
        budget_reserved=True,
        readiness_fresh=True,
        provenance_matched=True,
        driver_ready=True,
        driver_session_isolated=True,
        driver_disabled_by_default_proved=True,
        evidence_refs=("ev-gate-e1", "ev-driver-isolation"),
    )
    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: authorization},
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_ids={manifest.scenario.id: "run-1"},
        driver_revision="driver-source-a",
        now=now,
    )
    assert planned[0].blocked_reason is None

    mismatches = (
        (replace(authorization, scenario_version=2), "VERSION_MISMATCH"),
        (replace(authorization, scenario_content_hash="different"), "HASH_MISMATCH"),
        (replace(authorization, target_scope={"actor_ref": "other"}), "TARGET_SCOPE_MISMATCH"),
    )
    for invalid, reason in mismatches:
        planned = matrix.plan(
            [manifest.scenario.id],
            external_authorizations={manifest.scenario.id: invalid},
            tenant_id=DEFAULT_TENANT_ID,
            scenario_run_ids={manifest.scenario.id: "run-1"},
            driver_revision="driver-source-a",
            now=now,
        )
        assert planned[0].blocked_reason == f"EXTERNAL_AUTHORIZATION_{reason}"

    expired = replace(authorization, expires_at=now)
    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: expired},
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_ids={manifest.scenario.id: "run-1"},
        driver_revision="driver-source-a",
        now=now,
    )
    assert planned[0].blocked_reason == "EXTERNAL_EFFECT_PREREQUISITES_NOT_SATISFIED"

    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: authorization},
        tenant_id="tenant-other",
        scenario_run_ids={manifest.scenario.id: "run-1"},
        driver_revision="driver-source-a",
        now=now,
    )
    assert planned[0].blocked_reason == "EXTERNAL_AUTHORIZATION_TENANT_MISMATCH"

    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: authorization},
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_ids={manifest.scenario.id: "different-run"},
        driver_revision="driver-source-a",
        now=now,
    )
    assert planned[0].blocked_reason == "EXTERNAL_AUTHORIZATION_RUN_MISMATCH"

    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: authorization},
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_ids={manifest.scenario.id: "run-1"},
        driver_revision="different-driver",
        now=now,
    )
    assert planned[0].blocked_reason == "EXTERNAL_AUTHORIZATION_DRIVER_REVISION_MISMATCH"

    same_session = replace(authorization, owner_session_id="synthetic-session-ref")
    planned = matrix.plan(
        [manifest.scenario.id],
        external_authorizations={manifest.scenario.id: same_session},
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_ids={manifest.scenario.id: "run-1"},
        driver_revision="driver-source-a",
        now=now,
    )
    assert planned[0].blocked_reason == "EXTERNAL_EFFECT_PREREQUISITES_NOT_SATISFIED"


def test_external_scenario_run_requires_structural_non_owner_synthetic_actor(session):
    now = datetime.now(UTC)
    manifest = ScenarioManifest.model_validate(_manifest_data(external=True))
    version = register_manifest_version(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        manifest=manifest,
        manifest_source_path="config/platform/scenarios/SCN-PE-011.yaml",
        source_sha="source-a",
        now=now,
    )
    owner_collision = ActorBindingRow(
        id="binding-owner-collision",
        tenant_id=DEFAULT_TENANT_ID,
        source="synthetic-driver",
        external_actor_id="synthetic-owner-sentinel",
        actor_key="actor-owner",
        display_name="synthetic fixture",
        actor_category="owner",
        active_context=None,
        is_active=True,
        binding_metadata={"synthetic": True},
        created_at=now,
        updated_at=now,
    )
    session.add(owner_collision)
    session.flush()
    with pytest.raises(
        ScenarioContractError,
        match="SCENARIO_ACTOR_NOT_STRUCTURALLY_SYNTHETIC",
    ):
        create_scenario_run(
            session,
            run_id="run-owner-collision",
            tenant_id=DEFAULT_TENANT_ID,
            scenario_version_id=version.id,
            synthetic_actor_binding_id=owner_collision.id,
            root_correlation_id="correlation-owner-collision",
            source_sha="source-a",
            runtime_sha="runtime-a",
            schema_revision="0019",
            driver_revision="driver-a",
            readiness_result_id=None,
            effect_budget_id=None,
            expires_at=now + timedelta(minutes=5),
            require_synthetic_actor=True,
            now=now,
        )


def test_matrix_blocking_result_propagates():
    summary = AutomatedScenarioMatrix.summarize(
        [
            ScenarioExecutionResult(
                scenario_id="SCN-PE-002",
                status=MatrixItemStatus.PASS,
                reason_code="PASS",
                acceptance_ids=("ACC-CORE-LEASE-001",),
                invariant_ids=("INV-PE-LEASE-001",),
                risk_ids=("RPE-004",),
                evidence_refs=("ev-1",),
            ),
            ScenarioExecutionResult(
                scenario_id="SCN-PE-003",
                status=MatrixItemStatus.BLOCKED,
                reason_code="IMPLEMENTATION_MISSING",
                acceptance_ids=("ACC-CORE-BUDGET-002",),
                invariant_ids=("INV-PE-BUDGET-004",),
                risk_ids=("RPE-006",),
                evidence_refs=(),
            ),
        ]
    )
    assert summary.status is MatrixStatus.BLOCKED
    assert summary.pass_count == 1
    assert summary.blocked_count == 1
