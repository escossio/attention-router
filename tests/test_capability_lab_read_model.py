from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    EvidenceReferenceRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.application.platform.registry import ensure_default_tenant
from attention_router.platform.capability_lab_api import build_capability_lab_router
from attention_router.platform.capability_lab_read_model import read_scenario_engine_snapshot


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_empty_scenario_engine_snapshot_is_explicitly_observation_only(session):
    ensure_default_tenant(session)
    session.commit()

    snapshot = read_scenario_engine_snapshot(session)

    assert snapshot == {
        "read_only": True,
        "authority": "OBSERVATION_ONLY",
        "tenant_id": DEFAULT_TENANT_ID,
        "registered_scenario_count": 0,
        "recent_run_count": 0,
        "scenarios": [],
        "recent_runs": [],
    }
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_scenario_engine_snapshot_projects_existing_evidence_without_payloads(session):
    ensure_default_tenant(session)
    now = datetime.now(UTC)
    definition = ScenarioDefinitionRow(
        id="scenario-definition-lab",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_key="SCN-PE-999",
        title="Synthetic capability acceptance",
        description="Synthetic scenario used only to prove the read model.",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    version = ScenarioVersionRow(
        id="scenario-version-lab",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_definition_id=definition.id,
        version=1,
        schema_version="1",
        manifest_source_path="config/platform/scenarios/SCN-PE-999.yaml",
        content_hash="hash-synthetic-999",
        requirements_covered=["PE-999"],
        risk_ids=[],
        config_keys=[],
        risk_classification="P3",
        enabled=True,
        source_sha="source-synthetic",
        created_at=now,
        is_immutable=True,
        environment_classification="SYNTHETIC",
    )
    run = ScenarioRunRow(
        id="scenario-run-lab",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_version_id=version.id,
        synthetic_actor_binding_id=None,
        agent_execution_intent_id=None,
        status="PASSED",
        root_correlation_id="correlation-synthetic",
        source_sha="source-synthetic",
        runtime_sha="runtime-synthetic",
        schema_revision="schema-synthetic",
        driver_revision="driver-synthetic",
        readiness_result_id=None,
        effect_budget_id=None,
        created_at=now,
        validated_at=now,
        armed_at=now,
        started_at=now,
        verifying_at=now,
        completed_at=now,
        expires_at=now + timedelta(minutes=5),
        terminal_reason="SYNTHETIC_PASS",
        cleanup_state="COMPLETE",
        updated_at=now,
    )
    step = ScenarioStepRunRow(
        id="scenario-step-lab",
        tenant_id=DEFAULT_TENANT_ID,
        scenario_run_id=run.id,
        step_key="0:synthetic",
        step_order=0,
        step_kind="STIMULUS_DRY",
        status="PASSED",
        attempt=1,
        max_attempts=1,
        idempotency_key="scenario-run-lab:0:synthetic",
        correlation_id="correlation-synthetic",
        execution_lease_id=None,
        created_at=now,
        started_at=now,
        completed_at=now,
        updated_at=now,
        failure_reason=None,
        cleanup_state="COMPLETE",
    )
    evidence = EvidenceReferenceRow(
        id="evidence-lab",
        tenant_id=DEFAULT_TENANT_ID,
        evidence_type="SCENARIO_RUN",
        finding_id=None,
        finding_occurrence_id=None,
        internal_entity_type="scenario_run",
        internal_entity_id=run.id,
        external_reference=None,
        artifact_reference=None,
        trace_id=None,
        source_sha="source-synthetic",
        sanitized_metadata={"private_detail": "must-not-be-projected"},
        created_at=now,
    )
    session.add_all([definition, version, run, step, evidence])
    session.commit()

    before = {
        model: _count(session, model)
        for model in (
            ScenarioDefinitionRow,
            ScenarioVersionRow,
            ScenarioRunRow,
            ScenarioStepRunRow,
            EvidenceReferenceRow,
        )
    }
    snapshot = read_scenario_engine_snapshot(session)
    after = {model: _count(session, model) for model in before}

    assert before == after
    assert not session.new
    assert not session.dirty
    assert not session.deleted
    assert snapshot["read_only"] is True
    assert snapshot["authority"] == "OBSERVATION_ONLY"
    assert snapshot["registered_scenario_count"] == 1
    assert snapshot["recent_run_count"] == 1

    scenario = snapshot["scenarios"][0]
    assert scenario["scenario_key"] == "SCN-PE-999"
    assert scenario["latest_run"]["status"] == "PASSED"

    projected_run = snapshot["recent_runs"][0]
    assert projected_run["step_summary"] == {"total": 1, "statuses": {"PASSED": 1}}
    assert projected_run["evidence_refs"] == [
        {
            "id": "evidence-lab",
            "evidence_type": "SCENARIO_RUN",
            "source_sha": "source-synthetic",
            "created_at": now,
        }
    ]
    assert "sanitized_metadata" not in str(snapshot)
    assert "must-not-be-projected" not in str(snapshot)


def test_capability_lab_router_exposes_get_only_observation_surface():
    router = build_capability_lab_router(
        get_session=lambda: None,
        require_admin=lambda: None,
    )

    routes = [route for route in router.routes if hasattr(route, "methods")]
    assert len(routes) == 1
    assert routes[0].path == "/api/v1/admin/platform/capability-lab/scenario-engine"
    assert routes[0].methods == {"GET"}
