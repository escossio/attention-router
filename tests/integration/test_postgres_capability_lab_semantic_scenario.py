from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AssertionResultRow,
    CapabilityGrantRow,
    EvidenceReferenceRow,
    HumanExecutionAuthorizationRow,
    OutboxMessageRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
)
from attention_router.platform.capability_lab_semantic_executor import (
    execute_capability_lab_t0_manifest,
)
from attention_router.platform.scenarios import load_manifest_file


pytestmark = pytest.mark.postgres
MANIFEST_PATH = (
    Path(__file__).parents[2] / "config" / "platform" / "scenarios" / "SCN-PE-030.yaml"
)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_postgres_scn_pe_030_persists_real_semantic_evidence(Session):
    now = datetime(2026, 9, 10, 2, 15, tzinfo=UTC)
    manifest = load_manifest_file(MANIFEST_PATH)

    with Session() as session:
        sync_platform_registry(session)
        session.add(
            ActorBindingRow(
                id="pg-binding-capability-lab-semantic-control",
                tenant_id=DEFAULT_TENANT_ID,
                source="capability-lab-postgres-test",
                external_actor_id="synthetic-capability-lab-postgres-control",
                actor_key="actor_synthetic_test_actor",
                display_name="Synthetic Capability Lab PostgreSQL Actor",
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
        )
        session.commit()

        before_grants = _count(session, CapabilityGrantRow)
        before_human_auth = _count(session, HumanExecutionAuthorizationRow)
        before_outbox = _count(session, OutboxMessageRow)
        result = execute_capability_lab_t0_manifest(
            session,
            manifest=manifest,
            manifest_path="config/platform/scenarios/SCN-PE-030.yaml",
            source_sha="pg-source-semantic-030",
            runtime_sha="pg-runtime-semantic-030",
            schema_revision="0035_whatsapp_voice_media",
            now=now,
        )
        run_id = result.run_id
        assertion_id = result.assertion_result_id
        scenario_evidence_id = result.scenario_evidence_ref
        t0_evidence_id = result.t0_evidence_ref
        session.commit()

        assert _count(session, CapabilityGrantRow) == before_grants
        assert _count(session, HumanExecutionAuthorizationRow) == before_human_auth
        assert _count(session, OutboxMessageRow) == before_outbox

    with Session() as session:
        run = session.get(ScenarioRunRow, run_id)
        assert run is not None
        assert run.status == "PASSED"
        assert run.terminal_reason == "CAPABILITY_LAB_T0_SEMANTIC_PASS"
        assert run.source_sha == "pg-source-semantic-030"
        assert run.runtime_sha == "pg-runtime-semantic-030"

        step = session.scalar(
            select(ScenarioStepRunRow).where(ScenarioStepRunRow.scenario_run_id == run_id)
        )
        assert step is not None
        assert step.step_kind == "SEMANTIC_T0"
        assert step.status == "PASSED"

        assertion = session.get(AssertionResultRow, assertion_id)
        assert assertion is not None
        assert assertion.result == "PASS"
        assert assertion.observed_summary == (
            "comparison=PASS; authority=DENY; reason=CAPABILITY_GRANT_MISSING"
        )
        assert set(assertion.evidence_reference_ids) == {
            scenario_evidence_id,
            t0_evidence_id,
        }

        scenario_evidence = session.get(EvidenceReferenceRow, scenario_evidence_id)
        t0_evidence = session.get(EvidenceReferenceRow, t0_evidence_id)
        assert scenario_evidence is not None
        assert scenario_evidence.evidence_type == "SCENARIO_RUN"
        assert scenario_evidence.internal_entity_id == run_id
        assert t0_evidence is not None
        assert t0_evidence.evidence_type == "API_RESULT"
        assert t0_evidence.internal_entity_type == "operational_observation"

        assert _count(session, CapabilityGrantRow) == before_grants
        assert _count(session, HumanExecutionAuthorizationRow) == before_human_auth
        assert _count(session, OutboxMessageRow) == before_outbox
