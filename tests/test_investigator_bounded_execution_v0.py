"""Bounded SDK-call configuration and whole-process cancellation: offline only."""

import os
import sys
from types import SimpleNamespace

import pytest

from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime, InvestigatorReport, ModelPricing,
)
from attention_router.investigator.model import SourceEvent
from attention_router.investigator.model_usage_gate import (
    ModelBudgetDenied, ModelLimits, ModelUsage, ModelUsageGate,
)
from attention_router.investigator.process_supervisor import run_with_deadline
from attention_router.investigator.projection import IncidentProjection


def _event():
    return SourceEvent(
        "synthetic-zabbix", b"synthetic-input", "application/octet-stream",
        "synthetic-tenant", "synthetic-event",
    )


def _projection(event):
    return IncidentProjection(
        event_sha256=event.digest,
        symptom="resource_pressure",
        severity="warning",
        observed_at="2030-01-02T11:30:00Z",
        target_role="ci_worker",
        observed_value=87,
        unit="percent",
    )


def _runtime(**overrides):
    values = dict(
        model="synthetic-model",
        enable_provider=True,
        max_turns=4,
        model_attempt_timeout_seconds=4.0,
        max_output_tokens_per_attempt=96,
        projection_resolver=_projection,
        model_usage_gate=ModelUsageGate(ModelLimits(500, 400, 1000, 1)),
        model_quote=ModelUsage(400, 300, 700),
        model_pricing=ModelPricing(
            "synthetic-model", "synthetic-prices-only", 1_000_000, 1_000_000,
        ),
    )
    values.update(overrides)
    return AgentsSDKInvestigatorRuntime(**values)


def test_priced_sdk_run_has_pre_call_output_limit_timeout_no_parallel_tools(monkeypatch):
    from agents import Runner

    calls = []

    def offline_stub(agent, input, max_turns):
        settings = agent.model_settings
        assert settings.max_tokens == 75  # floor(300 quote / 4 turns)
        assert settings.timeout == 4.0
        assert settings.parallel_tool_calls is False
        assert settings.store is False
        assert len(agent.tools) == 1 and agent.tools[0].name == "read_observation"
        assert max_turns == 4
        assert "synthetic-tenant" not in str(input)
        calls.append("stub")
        return SimpleNamespace(
            final_output=InvestigatorReport(),
            context_wrapper=SimpleNamespace(usage=SimpleNamespace(
                requests=1, input_tokens=30, output_tokens=20,
            )),
        )

    monkeypatch.setattr(Runner, "run_sync", staticmethod(offline_stub))
    runtime = _runtime()
    result = runtime.investigate(_event(), None)
    assert result.evidence == ()
    assert calls == ["stub"]
    assert runtime.model_usage_gate.spent == ModelUsage(30, 20, 50)


@pytest.mark.parametrize("bad_attempt,bad_output", [
    (0, 128), (float("inf"), 128), (121.0, 128), (4.0, 0),
    (4.0, 2049), (4.0, True),
])
def test_malformed_preemptive_attempt_limits_rejected(bad_attempt, bad_output):
    with pytest.raises(ValueError, match="MODEL_ATTEMPT_LIMIT_INVALID"):
        _runtime(
            model_attempt_timeout_seconds=bad_attempt,
            max_output_tokens_per_attempt=bad_output,
        )


def test_whole_run_quote_with_too_few_output_tokens_is_rejected():
    with pytest.raises(ModelBudgetDenied, match="MODEL_QUOTE_INCOMPLETE"):
        _runtime(model_quote=ModelUsage(400, 3, 700))


def test_supervisor_exits_successfully_without_network_or_secrets():
    result = run_with_deadline(
        (sys.executable, "-c", "raise SystemExit(0)"),
        deadline_seconds=3,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.exit_code == 0
    assert not result.timed_out


def test_supervisor_terminates_hung_child_process_group():
    result = run_with_deadline(
        (sys.executable, "-c", "import time; time.sleep(15)"),
        deadline_seconds=0.15,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.timed_out and result.exit_code is None
    assert result.duration_seconds < 3


def test_supervisor_rejects_invalid_contract_without_starting_anything():
    with pytest.raises(ValueError, match="INVALID_SUPERVISOR_CONTRACT"):
        run_with_deadline(["/bin/echo", ""], deadline_seconds=3, env={})
    with pytest.raises(ValueError, match="INVALID_SUPERVISOR_CONTRACT"):
        run_with_deadline([sys.executable], deadline_seconds=float("nan"), env={})
