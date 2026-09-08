import pytest

from attention_router.platform.assertions import (
    AssertionBlocked,
    AssertionDefinition,
    AssertionOutcome,
    evaluate_predicate,
    require_blocking_assertions_pass,
)
from attention_router.platform.invariants import (
    CANONICAL_INVARIANT_IDS,
    CANONICAL_INVARIANTS,
    EvaluatedInvariant,
    InvariantBlocked,
    InvariantOutcome,
    InvariantRegistry,
)


def test_canonical_invariant_registry_has_exactly_54_unique_ids():
    registry = InvariantRegistry()
    registry.require_canonical_coverage()
    assert len(registry.all()) == 54
    assert len(CANONICAL_INVARIANT_IDS) == 54


def test_invariant_registry_rejects_duplicate_ids_from_generator():
    spec = CANONICAL_INVARIANTS[0]
    with pytest.raises(ValueError, match="DUPLICATE_INVARIANT_ID"):
        InvariantRegistry(item for item in (spec, spec))


def test_blocking_unknown_invariant_fails_closed():
    registry = InvariantRegistry()
    with pytest.raises(InvariantBlocked, match="INV-PE-LEASE-001"):
        registry.require_blocking_pass(
            [
                EvaluatedInvariant(
                    invariant_id="INV-PE-LEASE-001",
                    outcome=InvariantOutcome.UNKNOWN,
                    reason_code="DB_STATE_UNAVAILABLE",
                )
            ]
        )


def test_typed_assertion_result_and_blocking_semantics():
    definition = AssertionDefinition(
        assertion_id="semantic-owner-unavailable",
        version=1,
        assertion_type="SEMANTIC",
        expected_property="owner unavailable because sleeping",
        evaluator="andy-semantic-evaluator-v1",
        blocking=True,
    )
    result = evaluate_predicate(
        definition,
        observed={"classification": "generic"},
        predicate=lambda value: value["classification"] == "sleeping_unavailable",
        sanitized_actual_summary="generic response classification",
        evidence_refs=("ev-1",),
    )
    assert result.outcome is AssertionOutcome.FAIL
    with pytest.raises(AssertionBlocked, match="semantic-owner-unavailable"):
        require_blocking_assertions_pass([result])


def test_evaluator_error_becomes_unknown_not_optimistic_pass():
    definition = AssertionDefinition(
        assertion_id="deterministic-check",
        version=1,
        assertion_type="CONTRACT",
        expected_property="deterministic property",
        evaluator="contract-v1",
    )

    def broken(_observed):
        raise RuntimeError("sanitized failure")

    result = evaluate_predicate(definition, observed={}, predicate=broken)
    assert result.outcome is AssertionOutcome.UNKNOWN
    assert not result.permits_dispatch
