import pytest
from sqlalchemy import func, select

from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.capability_lab import CapabilityLabScenario, ComparisonStatus
from attention_router.infrastructure.models import (
    CapabilityGrantRow,
    EvidenceReferenceRow,
    HumanExecutionAuthorizationRow,
    OperationalObservationRow,
    ScenarioRunRow,
)
from attention_router.platform.capability_lab_probe import record_capability_t0_evidence


pytestmark = pytest.mark.postgres


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_postgres_durable_t0_evidence_survives_commit_without_authority_side_effects(Session):
    with Session() as session:
        sync_platform_registry(session)
        session.commit()

        scenario = CapabilityLabScenario(
            scenario_id="callback.request.postgres-deny-control",
            title="PostgreSQL durable T0 control",
            description="Prove the existing no-grant authority result with durable evidence.",
            capability_key="callback.request",
            requester_actor_key="synthetic:contact.postgres-control",
            request_text="Synthetic callback request",
            expected_resolution="DENY",
        )
        protected_before = {
            CapabilityGrantRow: _count(session, CapabilityGrantRow),
            HumanExecutionAuthorizationRow: _count(session, HumanExecutionAuthorizationRow),
            ScenarioRunRow: _count(session, ScenarioRunRow),
        }
        observation_before = _count(session, OperationalObservationRow)
        evidence_before = _count(session, EvidenceReferenceRow)

        report = record_capability_t0_evidence(
            session,
            scenario,
            source_sha="postgres-synthetic-source",
            runtime_sha="postgres-synthetic-runtime",
            schema_revision="postgres-migrated-schema",
        )
        observation_id = report.operational_observation_id
        evidence_id = report.evidence_reference_id
        session.commit()

    with Session() as session:
        assert report.certification == "DURABLE_T0_ONLY"
        assert report.probe.authority_result == "DENY"
        assert report.probe.reason_code == "CAPABILITY_GRANT_MISSING"
        assert report.comparison.status == ComparisonStatus.PASS
        assert report.comparison.evidence_refs == [evidence_id]

        for model, count in protected_before.items():
            assert _count(session, model) == count
        assert _count(session, OperationalObservationRow) == observation_before + 1
        assert _count(session, EvidenceReferenceRow) == evidence_before + 1

        operational = session.get(OperationalObservationRow, observation_id)
        evidence = session.get(EvidenceReferenceRow, evidence_id)
        assert operational is not None
        assert evidence is not None
        assert operational.source == "capability_lab_t0_semantic_evaluator"
        assert operational.source_type == "CAPABILITY_RESOLUTION"
        assert operational.lineage_classification == "SYNTHETIC"
        assert operational.status == "AVAILABLE_NOT_AUTHORIZED"
        assert operational.reason_code == "CAPABILITY_GRANT_MISSING"
        assert operational.sanitized_metadata == {
            "stage": "T0",
            "scenario_id": "callback.request.postgres-deny-control",
            "capability": "callback.request",
            "resolution_status": "AVAILABLE_NOT_AUTHORIZED",
            "authority_result": "DENY",
            "approval_required": False,
            "execution_allowed": False,
            "synthetic": True,
            "production_effects": False,
        }
        assert evidence.evidence_type == "API_RESULT"
        assert evidence.internal_entity_type == "operational_observation"
        assert evidence.internal_entity_id == operational.id
        assert evidence.sanitized_metadata == {
            "stage": "T0",
            "scenario_id": "callback.request.postgres-deny-control",
            "capability": "callback.request",
            "resolution_status": "AVAILABLE_NOT_AUTHORIZED",
            "authority_result": "DENY",
            "lineage": "SYNTHETIC",
        }
