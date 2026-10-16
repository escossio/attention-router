"""INV219: offline, no-provider test of SDK run quota admission and settlement."""

from types import SimpleNamespace

import pytest

from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime, InvestigatorReport, ModelPricing,
)
from attention_router.investigator.model import SourceEvent
from attention_router.investigator.model_usage_gate import (
    ModelBudgetDenied, ModelLimits, ModelUsage, ModelUsageGate,
)
from attention_router.investigator.projection import IncidentProjection


def _event():
    return SourceEvent(
        "synthetic-zabbix", b"synthetic-metadata-only", "application/octet-stream",
        "synthetic-tenant", "synthetic-event",
    )


def _project(event):
    return IncidentProjection(
        event_sha256=event.digest, symptom="resource_pressure",
        severity="warning", observed_at="2030-01-02T11:30:00Z",
        target_role="ci_worker", observed_value=87, unit="percent",
    )


def _priced(gate, *, quote=None, model="synthetic-model", pricing=None):
    return AgentsSDKInvestigatorRuntime(
        model=model, enable_provider=True, projection_resolver=_project,
        max_turns=4, model_usage_gate=gate,
        model_quote=quote or ModelUsage(400, 300, 700),
        model_pricing=pricing or ModelPricing(model, "fixture-rate-v1", 1_000_000, 1_000_000),
    )


def _gate():
    return ModelUsageGate(ModelLimits(500, 400, 1000, max_calls=1))


def _result(requests=2, input_tokens=60, output_tokens=45):
    return SimpleNamespace(
        final_output=InvestigatorReport(),
        context_wrapper=SimpleNamespace(
            usage=SimpleNamespace(
                requests=requests, input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        ),
    )


def test_no_quote_or_pricing_never_authorizes_provider():
    with pytest.raises(ModelBudgetDenied, match="MODEL_QUOTA_REQUIRED"):
        AgentsSDKInvestigatorRuntime(model="synthetic-model", enable_provider=True)
    with pytest.raises(ModelBudgetDenied, match="MODEL_PRICED_QUOTE_REQUIRED"):
        AgentsSDKInvestigatorRuntime(
            model="synthetic-model", enable_provider=True, model_usage_gate=_gate(),
        )
    with pytest.raises(ModelBudgetDenied, match="MODEL_PRICED_QUOTE_REQUIRED"):
        _priced(_gate(), pricing=ModelPricing("other-model", "fixture", 1, 1))


def test_underpriced_quote_rejected_at_construction():
    with pytest.raises(ModelBudgetDenied, match="MODEL_QUOTE_UNDERPRICED"):
        _priced(_gate(), quote=ModelUsage(400, 300, 1))


def test_reservation_denied_before_runner_even_if_sdk_is_available(monkeypatch):
    from agents import Runner

    called = []
    monkeypatch.setattr(Runner, "run_sync", staticmethod(
        lambda *args, **kwargs: called.append("CALLED")
    ))
    gate = ModelUsageGate(ModelLimits(50, 50, 100, max_calls=1))
    runtime = _priced(gate)
    with pytest.raises(ModelBudgetDenied, match="MODEL_BUDGET_EXCEEDED"):
        runtime.investigate(_event(), None)
    assert not called and gate.spent == ModelUsage(0, 0, 0)


def test_sdk_run_aggregate_usage_is_settled_with_versioned_prices(monkeypatch):
    from agents import Runner

    called = []
    def fake_run(agent, input, max_turns):
        called.append((agent.name, max_turns))
        assert "synthetic-tenant" not in str(input)
        return _result(requests=3, input_tokens=60, output_tokens=45)

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_run))
    gate = _gate()
    result = _priced(gate).investigate(_event(), None)
    assert result.hypotheses == ()
    assert called == [("ANDY_OPS_INVESTIGATOR_V0", 4)]
    assert gate.spent == ModelUsage(60, 45, 105)
    assert gate.terminal_reason is None
    with pytest.raises(ModelBudgetDenied, match="MODEL_CALL_LIMIT"):
        _priced(gate).investigate(_event(), None)
    assert len(called) == 1


@pytest.mark.parametrize("observed", [
    SimpleNamespace(final_output=InvestigatorReport()),
    _result(requests=0),
    _result(requests=5),
    _result(requests=1, input_tokens=-1),
    _result(requests=1, input_tokens=0, output_tokens=0),
])
def test_missing_invalid_or_out_of_bound_sdk_usage_closes_gate(monkeypatch, observed):
    from agents import Runner

    calls = []

    def fake_run(*args, **kwargs):
        calls.append("CALLED")
        return observed

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_run))
    gate = _gate()
    with pytest.raises(ModelBudgetDenied, match="MODEL_USAGE_UNKNOWN"):
        _priced(gate).investigate(_event(), None)
    assert gate.terminal_reason == "MODEL_USAGE_UNKNOWN"
    with pytest.raises(ModelBudgetDenied, match="MODEL_USAGE_UNKNOWN"):
        _priced(gate).investigate(_event(), None)
    assert calls == ["CALLED"]


def test_provider_exception_does_not_return_reservation_as_free(monkeypatch):
    from agents import Runner

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic model error must not be logged")

    monkeypatch.setattr(Runner, "run_sync", staticmethod(broken))
    gate = _gate()
    with pytest.raises(RuntimeError):
        _priced(gate).investigate(_event(), None)
    assert gate.terminal_reason == "MODEL_USAGE_UNKNOWN"
    with pytest.raises(ModelBudgetDenied, match="MODEL_USAGE_UNKNOWN"):
        _priced(gate).investigate(_event(), None)
