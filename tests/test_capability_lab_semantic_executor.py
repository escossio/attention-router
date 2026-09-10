from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AssertionResultRow,
    CapabilityGrantRow,
    EvidenceReferenceRow,
    HumanExecutionAuthorizationRow,
    OperationalObservationRow,
    OutboxMessageRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.platform.capability_lab_semantic_executor import (
    execute_capability_lab_t0_manifest,
)
from attention_router.platform.scenarios import ScenarioManifest, load_manifest_file


MANIFEST_PATH = (
    Path(__file__).parents[1] / "config" / "platform" / "scenarios" / "SCN-PE-030.yaml"
)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _seed_synthetic_actor(session, *, now: datetime) -> ActorBindingRow:
    row = ActorBindingRow(
        id="binding-capability-lab-semantic-control",
        tenant_id=DEFAULT_TENANT_ID,
        source="capability-lab-test",
        external_actor_id="synthetic-capability-lab-control",
        actor_key="actor_synthetic_test_actor",
        display_name="Synthetic Capability Lab Actor",
        actor_category="SYNTHETIC_TEST_ACTOR",
        active_context=None,
        is_active=True,
        binding_metadata={
            "synthetic": True,
            "lineage_classification": "SYNTHETIC",
        },
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _prepare(session, *, now: datetime) -> None:
    sync_platform_registry(session)
    _seed_synthetic_actor(session, now=now)
    session.flush()


def test_scn_pe_030_binds_scenario_engine_to_real_t0_semantics(session):
    now = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
    _prepare(session, now=now)
    manifest = load_manifest_file(MANIFEST_PATH)
    before_grants = _count(session, CapabilityGrantRow)
    before_human_auth = _count(session, HumanExecutionAuthorizationRow)
    before_outbox = _count(session, OutboxMessageRow)

    result = execute_capability_lab_t0_manifest(
        session,
        manifest=manifest,
        manifest_path="config/platform/scenarios/SCN-PE-030.yaml",
        source_sha="source-semantic-030",
        runtime_sha="runtime-semantic-030",
        schema_revision="0035_whatsapp_voice_media",
        now=now,
    )

    assert result.scenario_key == "SCN-PE-030"
    assert result.status == "PASSED"
    assert result.reason_code == "CAPABILITY_LAB_T0_SEMANTIC_PASS"
    assert _count(session, CapabilityGrantRow) == before_grants
    assert _count(session, HumanExecutionAuthorizationRow) == before_human_auth
    assert _count(session, OutboxMessageRow) == before_outbox

    run = session.get(ScenarioRunRow, result.run_id)
    assert run is not None
    assert run.status == "PASSED"
    assert run.terminal_reason == "CAPABILITY_LAB_T0_SEMANTIC_PASS"
    assert run.synthetic_actor_binding_id == "binding-capability-lab-semantic-control"

    version = session.get(ScenarioVersionRow, run.scenario_version_id)
    assert version is not None
    definition = session.get(ScenarioDefinitionRow, version.scenario_definition_id)
    assert definition is not None
    assert definition.scenario_key == "SCN-PE-030"

    step = session.scalar(
        select(ScenarioStepRunRow).where(ScenarioStepRunRow.scenario_run_id == run.id)
    )
    assert step is not None
    assert step.step_kind == "SEMANTIC_T0"
    assert step.status == "PASSED"

    assertion = session.get(AssertionResultRow, result.assertion_result_id)
    assert assertion is not None
    assert assertion.result == "PASS"
    assert assertion.evaluator == "capability_lab_t0_semantic_evaluator"
    assert set(assertion.evidence_reference_ids) == {
        result.scenario_evidence_ref,
        result.t0_evidence_ref,
    }
    assert assertion.observed_summary == (
        "comparison=PASS; authority=DENY; reason=CAPABILITY_GRANT_MISSING"
    )

    scenario_evidence = session.get(EvidenceReferenceRow, result.scenario_evidence_ref)
    assert scenario_evidence is not None
    assert scenario_evidence.evidence_type == "SCENARIO_RUN"
    assert scenario_evidence.internal_entity_type == "scenario_run"
    assert scenario_evidence.internal_entity_id == run.id

    t0_evidence = session.get(EvidenceReferenceRow, result.t0_evidence_ref)
    assert t0_evidence is not None
    assert t0_evidence.evidence_type == "API_RESULT"
    assert t0_evidence.internal_entity_type == "operational_observation"
    operational = session.get(OperationalObservationRow, t0_evidence.internal_entity_id)
    assert operational is not None
    assert operational.source == "capability_lab_t0_semantic_evaluator"
    assert operational.status == "AVAILABLE_NOT_AUTHORIZED"
    assert operational.reason_code == "CAPABILITY_GRANT_MISSING"
    assert operational.lineage_classification == "SYNTHETIC"
    assert operational.sanitized_metadata["authority_result"] == "DENY"
    assert operational.sanitized_metadata["production_effects"] is False


def test_semantic_scenario_fails_when_reason_code_does_not_match_runtime(session):
    now = datetime(2026, 9, 10, 2, 5, tzinfo=UTC)
    _prepare(session, now=now)
    canonical = load_manifest_file(MANIFEST_PATH)
    payload = canonical.model_dump(mode="json")
    payload["execution"]["stimulus"][0]["parameters"][
        "expected_reason_code"
    ] = "SYNTHETIC_WRONG_REASON"
    manifest = ScenarioManifest.model_validate(payload)

    result = execute_capability_lab_t0_manifest(
        session,
        manifest=manifest,
        manifest_path="tests/synthetic/SCN-PE-030-wrong-reason.yaml",
        source_sha="source-semantic-030-mismatch",
        runtime_sha="runtime-semantic-030-mismatch",
        schema_revision="0035_whatsapp_voice_media",
        now=now,
    )

    assert result.status == "FAILED"
    assert result.reason_code == "CAPABILITY_LAB_T0_SEMANTIC_MISMATCH"
    run = session.get(ScenarioRunRow, result.run_id)
    assertion = session.get(AssertionResultRow, result.assertion_result_id)
    assert run is not None and run.status == "FAILED"
    assert assertion is not None and assertion.result == "FAIL"
    assert assertion.observed_summary == (
        "comparison=PASS; authority=DENY; reason=CAPABILITY_GRANT_MISSING"
    )
