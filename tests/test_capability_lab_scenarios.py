import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from attention_router.core.authority import AuthorityResult, evaluate_effective_authority
from attention_router.core.capabilities import CapabilityAvailability
from attention_router.core.capability_lab import (
    CapabilityLabObservation,
    CapabilityLabScenario,
    ComparisonStatus,
    compare_scenario,
)


FIXTURE = Path("attention_router/web/static/capability-lab-scenarios.json")


def _scenarios() -> list[CapabilityLabScenario]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [CapabilityLabScenario.model_validate(item) for item in payload]


def _cpf_scenario() -> CapabilityLabScenario:
    return {
        scenario.scenario_id: scenario for scenario in _scenarios()
    }["personal.identity.cpf.requires-approval-once"]


def test_capability_lab_fixture_is_valid_and_synthetic_only():
    scenarios = _scenarios()

    assert len(scenarios) >= 3
    assert len({scenario.scenario_id for scenario in scenarios}) == len(scenarios)
    assert all(scenario.synthetic_only for scenario in scenarios)
    assert all(not scenario.uses_real_personal_data for scenario in scenarios)
    assert all(not scenario.production_effects_allowed for scenario in scenarios)
    assert all(scenario.requester_actor_key.startswith("synthetic:") for scenario in scenarios)
    assert all(scenario.engine_scenario_key is None for scenario in scenarios)


def test_capability_lab_fixture_covers_allowance_boundaries_without_real_values():
    scenarios = _scenarios()
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}

    cpf = by_id["personal.identity.cpf.requires-approval-once"]
    relationship = by_id["personal.relationship.status.denied"]
    location = by_id["location.current.temporary-grant"]

    assert cpf.expected_resolution == "REQUIRES_APPROVAL"
    assert cpf.expected_grant_mode == "ONE_TIME"
    assert relationship.expected_resolution == "DENY"
    assert relationship.expected_grant_mode == "NONE"
    assert location.expected_resolution == "REQUIRES_APPROVAL"
    assert location.expected_grant_mode == "TIME_BOUND"

    serialized = FIXTURE.read_text(encoding="utf-8")
    assert "000.000.000-00" not in serialized
    assert "latitude" not in serialized.lower()
    assert "longitude" not in serialized.lower()


def test_lab_reuses_canonical_capability_name_validation():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    payload["capability_key"] = " Personal.Identity.CPF "

    scenario = CapabilityLabScenario.model_validate(payload)

    assert scenario.capability_key == "personal.identity.cpf"

    payload["capability_key"] = "cpf"
    with pytest.raises(ValidationError):
        CapabilityLabScenario.model_validate(payload)


def test_non_approval_scenario_cannot_simulate_human_decision():
    with pytest.raises(ValidationError):
        CapabilityLabScenario(
            scenario_id="invalid.deny-with-approval",
            title="Invalid fixture",
            description="A deny path cannot also pretend that a human approval occurred.",
            capability_key="personal.relationship.status",
            requester_actor_key="synthetic:contact.invalid",
            request_text="Synthetic request",
            expected_resolution="DENY",
            simulated_human_decision="APPROVE",
        )


def test_pending_approval_scenario_cannot_expect_grant():
    with pytest.raises(ValidationError):
        CapabilityLabScenario(
            scenario_id="invalid.pending-with-grant",
            title="Invalid pending grant",
            description="A pending authorization cannot already carry a derived permission.",
            capability_key="personal.identity.cpf",
            requester_actor_key="synthetic:contact.invalid",
            request_text="Synthetic request",
            expected_resolution="REQUIRES_APPROVAL",
            simulated_human_decision="NONE",
            expected_grant_mode="PERSISTENT",
        )


def test_later_stage_observation_requires_t0_resolution():
    with pytest.raises(ValidationError):
        CapabilityLabObservation(
            scenario_id=_cpf_scenario().scenario_id,
            observed_human_decision="APPROVE",
        )

    with pytest.raises(ValidationError):
        CapabilityLabObservation(
            scenario_id=_cpf_scenario().scenario_id,
            observed_grant_mode="ONE_TIME",
        )


def test_stage_evidence_cannot_exist_without_its_observation():
    with pytest.raises(ValidationError):
        CapabilityLabObservation(
            scenario_id=_cpf_scenario().scenario_id,
            resolution_evidence_refs=["evidence:t0"],
        )

    with pytest.raises(ValidationError):
        CapabilityLabObservation(
            scenario_id=_cpf_scenario().scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            human_decision_evidence_refs=["evidence:t1"],
        )

    with pytest.raises(ValidationError):
        CapabilityLabObservation(
            scenario_id=_cpf_scenario().scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            grant_evidence_refs=["evidence:t2"],
        )


def test_current_no_grant_authority_gap_is_explicit_not_hidden():
    cpf = _cpf_scenario()

    current = evaluate_effective_authority(
        availability=CapabilityAvailability.OPERATIONAL,
        policy_allows=True,
        grant_active=False,
        side_effect=False,
        default_approval_policy="REQUIRES_APPROVAL",
    )

    assert cpf.expected_resolution == AuthorityResult.REQUIRES_APPROVAL
    assert current.result == AuthorityResult.DENY
    assert current.reason_code == "CAPABILITY_GRANT_MISSING"


def test_matching_t0_without_evidence_is_incomplete():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
        ),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.incomplete_reasons == [
        "T0_RESOLUTION_EVIDENCE_MISSING",
        "T1_HUMAN_DECISION_NOT_OBSERVED",
    ]
    assert comparison.evidence_refs == []


def test_t0_matching_resolution_with_evidence_waits_for_human_decision():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_reason_code="APPROVAL_REQUIRED",
            resolution_evidence_refs=["synthetic:evidence:t0"],
        ),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == ["T1_HUMAN_DECISION_NOT_OBSERVED"]
    assert comparison.evidence_refs == ["synthetic:evidence:t0"]


def test_t1_matching_approval_without_evidence_remains_incomplete():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="APPROVE",
            resolution_evidence_refs=["synthetic:evidence:t0"],
        ),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == [
        "T1_HUMAN_DECISION_EVIDENCE_MISSING",
        "T2_GRANT_NOT_OBSERVED",
    ]


def test_t1_matching_approval_with_evidence_waits_for_expected_grant():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="APPROVE",
            resolution_evidence_refs=["synthetic:evidence:t0"],
            human_decision_evidence_refs=["synthetic:evidence:t1"],
        ),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == ["T2_GRANT_NOT_OBSERVED"]
    assert comparison.evidence_refs == [
        "synthetic:evidence:t0",
        "synthetic:evidence:t1",
    ]


def test_t2_matching_grant_without_evidence_is_not_certified():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="APPROVE",
            observed_grant_mode="ONE_TIME",
            resolution_evidence_refs=["synthetic:evidence:t0"],
            human_decision_evidence_refs=["synthetic:evidence:t1"],
        ),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.incomplete_reasons == ["T2_GRANT_EVIDENCE_MISSING"]


def test_t2_matching_grant_with_all_stage_evidence_completes_flow():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="APPROVE",
            observed_grant_mode="ONE_TIME",
            matched_rule_id="synthetic-rule-1",
            resolution_evidence_refs=["synthetic:evidence:t0"],
            human_decision_evidence_refs=["synthetic:evidence:t1"],
            grant_evidence_refs=["synthetic:evidence:t2"],
        ),
    )

    assert comparison.status == ComparisonStatus.PASS
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == []
    assert comparison.evidence_refs == [
        "synthetic:evidence:t0",
        "synthetic:evidence:t1",
        "synthetic:evidence:t2",
    ]


def test_missing_t0_observation_is_incomplete_not_success():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(scenario_id=scenario.scenario_id),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []
    assert comparison.incomplete_reasons == ["T0_RESOLUTION_NOT_OBSERVED"]


def test_t0_resolution_mismatch_fails_without_guessing_downstream_state():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="DENY",
            observed_reason_code="CAPABILITY_GRANT_MISSING",
            resolution_evidence_refs=["synthetic:evidence:t0"],
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert len(comparison.mismatches) == 1
    assert comparison.mismatches[0].startswith("RESOLUTION_MISMATCH:")
    assert comparison.incomplete_reasons == []


def test_t1_human_decision_mismatch_fails_before_grant_stage():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="DENY",
            resolution_evidence_refs=["synthetic:evidence:t0"],
            human_decision_evidence_refs=["synthetic:evidence:t1"],
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert any(item.startswith("HUMAN_DECISION_MISMATCH:") for item in comparison.mismatches)
    assert comparison.incomplete_reasons == []


def test_t2_wrong_grant_mode_fails_with_reason_code():
    scenario = _cpf_scenario()
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="REQUIRES_APPROVAL",
            observed_human_decision="APPROVE",
            observed_grant_mode="PERSISTENT",
            resolution_evidence_refs=["synthetic:evidence:t0"],
            human_decision_evidence_refs=["synthetic:evidence:t1"],
            grant_evidence_refs=["synthetic:evidence:t2"],
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert any(item.startswith("GRANT_MODE_MISMATCH:") for item in comparison.mismatches)


def test_direct_deny_needs_t0_evidence_to_complete():
    scenario = {
        item.scenario_id: item for item in _scenarios()
    }["personal.relationship.status.denied"]

    without_evidence = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="DENY",
        ),
    )
    assert without_evidence.status == ComparisonStatus.INCOMPLETE
    assert without_evidence.incomplete_reasons == ["T0_RESOLUTION_EVIDENCE_MISSING"]

    with_evidence = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="DENY",
            observed_reason_code="POLICY_DENIED",
            resolution_evidence_refs=["synthetic:evidence:t0"],
        ),
    )
    assert with_evidence.status == ComparisonStatus.PASS
    assert with_evidence.mismatches == []
    assert with_evidence.incomplete_reasons == []


def test_any_production_effect_observed_fails_the_lab_comparison():
    scenario = {
        item.scenario_id: item for item in _scenarios()
    }["personal.relationship.status.denied"]
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution=scenario.expected_resolution,
            resolution_evidence_refs=["synthetic:evidence:t0"],
            production_effect_observed=True,
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert "PRODUCTION_EFFECT_OBSERVED" in comparison.mismatches
