import json
from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_capability_lab_fixture_is_valid_and_synthetic_only():
    scenarios = _scenarios()

    assert len(scenarios) >= 3
    assert len({scenario.scenario_id for scenario in scenarios}) == len(scenarios)
    assert all(scenario.synthetic_only for scenario in scenarios)
    assert all(not scenario.uses_real_personal_data for scenario in scenarios)
    assert all(not scenario.production_effects_allowed for scenario in scenarios)
    assert all(scenario.requester_actor_key.startswith("synthetic:") for scenario in scenarios)


def test_capability_lab_fixture_covers_allowance_boundaries_without_real_values():
    scenarios = _scenarios()
    by_id = {scenario.scenario_id: scenario for scenario in scenarios}

    cpf = by_id["personal.identity.cpf.requires-approval-once"]
    relationship = by_id["personal.relationship.status.denied"]
    location = by_id["location.current.temporary-grant"]

    assert cpf.expected_resolution == "REQUIRE_APPROVAL"
    assert cpf.expected_grant_mode == "ONE_TIME"
    assert relationship.expected_resolution == "DENY"
    assert relationship.expected_grant_mode == "NONE"
    assert location.expected_resolution == "REQUIRE_APPROVAL"
    assert location.expected_grant_mode == "TIME_BOUND"

    serialized = FIXTURE.read_text(encoding="utf-8")
    assert "000.000.000-00" not in serialized
    assert "latitude" not in serialized.lower()
    assert "longitude" not in serialized.lower()


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


def test_denied_or_pending_scenario_cannot_expect_grant():
    with pytest.raises(ValidationError):
        CapabilityLabScenario(
            scenario_id="invalid.pending-with-grant",
            title="Invalid pending grant",
            description="A pending authorization cannot already carry a derived permission.",
            capability_key="personal.identity.cpf",
            requester_actor_key="synthetic:contact.invalid",
            request_text="Synthetic request",
            expected_resolution="REQUIRE_APPROVAL",
            simulated_human_decision="NONE",
            expected_grant_mode="PERSISTENT",
        )


def test_expected_and_observed_match_passes_without_mutation():
    scenario = _scenarios()[0]
    observation = CapabilityLabObservation(
        scenario_id=scenario.scenario_id,
        observed_resolution=scenario.expected_resolution,
        observed_grant_mode=scenario.expected_grant_mode,
        matched_rule_id="synthetic-rule-1",
        evidence_refs=["synthetic:evidence:1"],
    )

    comparison = compare_scenario(scenario, observation)

    assert comparison.status == ComparisonStatus.PASS
    assert comparison.mismatches == []
    assert comparison.evidence_refs == ["synthetic:evidence:1"]


def test_missing_observation_is_incomplete_not_success():
    scenario = _scenarios()[0]
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(scenario_id=scenario.scenario_id),
    )

    assert comparison.status == ComparisonStatus.INCOMPLETE
    assert comparison.mismatches == []


def test_resolution_or_grant_mismatch_fails_with_reason_codes():
    scenario = _scenarios()[0]
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution="DENY",
            observed_grant_mode="NONE",
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert any(item.startswith("RESOLUTION_MISMATCH:") for item in comparison.mismatches)
    assert any(item.startswith("GRANT_MODE_MISMATCH:") for item in comparison.mismatches)


def test_any_production_effect_observed_fails_the_lab_comparison():
    scenario = _scenarios()[1]
    comparison = compare_scenario(
        scenario,
        CapabilityLabObservation(
            scenario_id=scenario.scenario_id,
            observed_resolution=scenario.expected_resolution,
            production_effect_observed=True,
        ),
    )

    assert comparison.status == ComparisonStatus.FAIL
    assert "PRODUCTION_EFFECT_OBSERVED" in comparison.mismatches
