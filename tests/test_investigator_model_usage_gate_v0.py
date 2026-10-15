"""Offline model cost/token quota tests; no SDK, API key, or network."""

import pytest

from attention_router.investigator.model_usage_gate import (
    ModelBudgetDenied, ModelLimits, ModelUsage, ModelUsageGate,
)


def _gate():
    return ModelUsageGate(ModelLimits(
        input_tokens=100, output_tokens=40, cost_micro_usd=10000, max_calls=3,
    ))


def test_reservation_then_settlement_consumes_tokens_and_micro_usd():
    gate = _gate()
    gate.reserve("call-a", ModelUsage(40, 20, 3000))
    gate.settle("call-a", ModelUsage(30, 15, 2100))
    assert gate.spent == ModelUsage(30, 15, 2100)
    gate.reserve("call-b", ModelUsage(50, 15, 4000))
    gate.settle("call-b", ModelUsage(50, 15, 4000))
    assert gate.spent == ModelUsage(80, 30, 6100)
    assert gate.terminal_reason is None


def test_concurrent_reservations_consume_capacity_before_model_request():
    gate = _gate()
    gate.reserve("call-a", ModelUsage(70, 20, 7000))
    with pytest.raises(ModelBudgetDenied, match="MODEL_BUDGET_EXCEEDED"):
        gate.reserve("call-b", ModelUsage(40, 10, 4000))
    gate.settle("call-a", ModelUsage(55, 10, 4500))
    gate.reserve("call-b", ModelUsage(40, 20, 4000))
    assert gate.terminal_reason is None


def test_unknown_usage_is_terminal_not_free():
    gate = _gate()
    gate.reserve("call-a", ModelUsage(20, 10, 1000))
    gate.mark_failed_without_usage("call-a")
    assert gate.terminal_reason == "MODEL_USAGE_UNKNOWN"
    assert gate.spent == ModelUsage(0, 0, 0)  # Not a billing claim.
    with pytest.raises(ModelBudgetDenied, match="MODEL_USAGE_UNKNOWN"):
        gate.reserve("call-b", ModelUsage(1, 1, 1))


def test_provider_overrun_is_accounted_and_blocks_future_calls():
    gate = _gate()
    gate.reserve("call-a", ModelUsage(20, 10, 1000))
    with pytest.raises(ModelBudgetDenied, match="MODEL_RESERVATION_OVERRUN"):
        gate.settle("call-a", ModelUsage(25, 10, 2000))
    assert gate.spent == ModelUsage(25, 10, 2000)
    with pytest.raises(ModelBudgetDenied, match="MODEL_RESERVATION_OVERRUN"):
        gate.reserve("call-b", ModelUsage(1, 1, 1))


def test_duplicate_id_and_double_settlement_fail_closed():
    gate = _gate()
    gate.reserve("call-a", ModelUsage(10, 10, 500))
    with pytest.raises(ModelBudgetDenied, match="MODEL_CALL_REPLAY"):
        gate.reserve("call-a", ModelUsage(10, 10, 500))
    gate.settle("call-a", ModelUsage(10, 10, 500))
    with pytest.raises(ModelBudgetDenied, match="MODEL_CALL_NOT_RESERVED"):
        gate.settle("call-a", ModelUsage(10, 10, 500))
    with pytest.raises(ModelBudgetDenied, match="MODEL_CALL_REPLAY"):
        gate.reserve("call-a", ModelUsage(1, 1, 1))


def test_max_calls_counts_reserved_attempts():
    gate = ModelUsageGate(ModelLimits(100, 40, 10000, max_calls=1))
    gate.reserve("first", ModelUsage(10, 5, 100))
    gate.settle("first", ModelUsage(10, 5, 100))
    with pytest.raises(ModelBudgetDenied, match="MODEL_CALL_LIMIT"):
        gate.reserve("second", ModelUsage(1, 1, 1))


@pytest.mark.parametrize("bad", [
    (-1, 2, 0), (1, -1, 0), (1, 2, -1), (True, 2, 0),
])
def test_invalid_usage_is_rejected(bad):
    with pytest.raises(ValueError, match="MODEL_USAGE_INVALID"):
        ModelUsage(*bad)


def test_unknown_quote_and_invalid_limits_never_authorize_model():
    gate = _gate()
    with pytest.raises(ModelBudgetDenied, match="MODEL_QUOTE_REQUIRED"):
        gate.reserve("call", None)
    with pytest.raises(ValueError, match="MODEL_LIMIT_INVALID"):
        ModelLimits(100, 0, 500, max_calls=3)
    assert gate.spent == ModelUsage(0, 0, 0)
