from attention_router.platform.acceptance import (
    ACCEPTANCE_REGISTRY,
    AcceptanceStatus,
    AcceptanceVehicle,
    classify_acceptance,
    validate_acceptance_registry,
)


def test_acceptance_registry_has_exact_canonical_cardinality():
    validate_acceptance_registry()
    assert len(ACCEPTANCE_REGISTRY) == 128
    assert len(set(ACCEPTANCE_REGISTRY)) == 128


def test_documented_or_unevaluated_criterion_cannot_pass():
    result = classify_acceptance(
        "ACC-CORE-LEASE-001",
        evaluated=False,
        expected_property_satisfied=True,
    )
    assert result.status is AcceptanceStatus.BLOCKED
    assert result.reason_code == "NOT_EXECUTED"


def test_pass_requires_evidence_and_an_observed_expected_property():
    missing = classify_acceptance(
        "ACC-CORE-LEASE-001",
        evaluated=True,
        expected_property_satisfied=True,
    )
    assert missing.status is AcceptanceStatus.BLOCKED
    passed = classify_acceptance(
        "ACC-CORE-LEASE-001",
        evaluated=True,
        expected_property_satisfied=True,
        evidence_references=["test-report:lease-single-winner"],
    )
    assert passed.status is AcceptanceStatus.PASS


def test_human_and_post_apply_criteria_are_not_collapsed_into_scenarios():
    assert ACCEPTANCE_REGISTRY["ACC-PE017-002"].primary_vehicle is AcceptanceVehicle.HUMAN_GOVERNANCE
    assert ACCEPTANCE_REGISTRY["ACC-PE017-008"].primary_vehicle is AcceptanceVehicle.POST_APPLY
