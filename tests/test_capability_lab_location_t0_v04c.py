from sqlalchemy import func, select

from attention_router.application.platform.capability_pack import provision_internal_providers
from attention_router.core.capability_lab import T0ComparisonStatus
from attention_router.infrastructure.models import (
    CapabilityGrantRow,
    EvidenceReferenceRow,
    HumanExecutionAuthorizationRow,
    OperationalObservationRow,
    ScenarioRunRow,
)
from attention_router.platform.capability_lab_probe import (
    capability_lab_t0_scenario,
    load_capability_lab_t0_scenarios,
    probe_named_capability_t0,
    record_named_capability_t0_evidence,
)


SCENARIO_ID = "location.current.no-grant"


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _prepare_location_runtime(session) -> None:
    provision_internal_providers(session, "00000000-0000-4000-8000-000000000001")
    session.commit()
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_fixture_contains_only_repository_owned_current_location_t0():
    scenarios = load_capability_lab_t0_scenarios()

    assert set(scenarios) == {SCENARIO_ID}
    scenario = scenarios[SCENARIO_ID]
    assert scenario.capability_key == "location.current"
    assert scenario.synthetic_only is True
    assert scenario.production_effects_allowed is False
    assert scenario.expected_resolution_status == "AVAILABLE_NOT_AUTHORIZED"
    assert scenario.expected_authority_result == "DENY"
    assert scenario.expected_reason_code == "CAPABILITY_GRANT_MISSING"


def test_unknown_t0_scenario_fails_closed():
    try:
        capability_lab_t0_scenario("location.current.unknown")
    except KeyError as exc:
        assert exc.args == ("CAPABILITY_LAB_T0_SCENARIO_UNKNOWN",)
    else:  # pragma: no cover
        raise AssertionError("unknown scenario must fail closed")


def test_ephemeral_location_t0_proves_operational_provider_but_no_grant(session):
    _prepare_location_runtime(session)
    protected_before = {
        CapabilityGrantRow: _count(session, CapabilityGrantRow),
        HumanExecutionAuthorizationRow: _count(session, HumanExecutionAuthorizationRow),
        ScenarioRunRow: _count(session, ScenarioRunRow),
        OperationalObservationRow: _count(session, OperationalObservationRow),
        EvidenceReferenceRow: _count(session, EvidenceReferenceRow),
    }

    report = probe_named_capability_t0(session, SCENARIO_ID)

    assert report.certification == "EPHEMERAL_T0_ONLY"
    assert report.observation.resolution_status == "AVAILABLE_NOT_AUTHORIZED"
    assert report.observation.authority_result == "DENY"
    assert report.observation.reason_code == "CAPABILITY_GRANT_MISSING"
    assert report.observation.provider_interface == "LocationProvider"
    assert report.observation.approval_required is False
    assert report.observation.execution_allowed is False
    assert report.comparison.status == T0ComparisonStatus.PASS
    assert report.comparison.evidence_refs == []

    for model, count in protected_before.items():
        assert _count(session, model) == count


def test_durable_location_t0_certifies_semantics_without_authority_side_effects(session):
    _prepare_location_runtime(session)
    protected_before = {
        CapabilityGrantRow: _count(session, CapabilityGrantRow),
        HumanExecutionAuthorizationRow: _count(session, HumanExecutionAuthorizationRow),
        ScenarioRunRow: _count(session, ScenarioRunRow),
    }
    observation_before = _count(session, OperationalObservationRow)
    evidence_before = _count(session, EvidenceReferenceRow)

    report = record_named_capability_t0_evidence(
        session,
        SCENARIO_ID,
        source_sha="v04c-source",
        runtime_sha="v04c-runtime",
        schema_revision="capability-lab.t0.v1",
    )
    session.commit()

    assert report.certification == "DURABLE_T0_ONLY"
    assert report.comparison.status == T0ComparisonStatus.PASS
    assert report.comparison.evidence_refs == [report.evidence_reference_id]
    assert report.observation.provider_interface == "LocationProvider"

    for model, count in protected_before.items():
        assert _count(session, model) == count
    assert _count(session, OperationalObservationRow) == observation_before + 1
    assert _count(session, EvidenceReferenceRow) == evidence_before + 1

    operational = session.get(OperationalObservationRow, report.operational_observation_id)
    evidence = session.get(EvidenceReferenceRow, report.evidence_reference_id)
    assert operational is not None
    assert evidence is not None
    assert operational.source == "capability_lab_t0"
    assert operational.status == "AVAILABLE_NOT_AUTHORIZED"
    assert operational.reason_code == "CAPABILITY_GRANT_MISSING"
    assert operational.lineage_classification == "SYNTHETIC"
    assert operational.sanitized_metadata["capability"] == "location.current"
    assert operational.sanitized_metadata["provider_interface"] == "LocationProvider"
    assert operational.sanitized_metadata["production_effects"] is False
    assert evidence.evidence_type == "API_RESULT"
    assert evidence.internal_entity_type == "operational_observation"
    assert evidence.internal_entity_id == operational.id


def test_durable_location_t0_requires_explicit_provenance(session):
    _prepare_location_runtime(session)

    try:
        record_named_capability_t0_evidence(
            session,
            SCENARIO_ID,
            source_sha="",
            runtime_sha="runtime",
            schema_revision="schema",
        )
    except ValueError as exc:
        assert str(exc) == "CAPABILITY_LAB_T0_PROVENANCE_REQUIRED"
    else:  # pragma: no cover
        raise AssertionError("durable T0 evidence must require provenance")
