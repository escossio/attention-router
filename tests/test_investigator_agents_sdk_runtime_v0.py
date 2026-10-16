"""Offline integration checks for the pinned Agents SDK public function-tool API."""

import asyncio
import json
from types import SimpleNamespace

from attention_router.investigator.model_usage_gate import (
    ModelLimits, ModelUsage, ModelUsageGate,
)
from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime, HypothesisReport, InvestigatorReport, ModelPricing,
)
from attention_router.investigator.model import SourceEvent
from attention_router.investigator.projection import IncidentProjection
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import MemoryAudit, MemoryStore, SimulatedConnector


def _guarded_runtime(**kwargs):
    """Use priced, bounded synthetic quotas in every offline SDK-stub test."""
    if kwargs.get("enable_provider"):
        model = kwargs["model"]
        kwargs.setdefault("model_usage_gate", ModelUsageGate(
            ModelLimits(5000, 2000, 10000, max_calls=1),
        ))
        kwargs.setdefault("model_quote", ModelUsage(3000, 1000, 4000))
        kwargs.setdefault("model_pricing", ModelPricing(
            model, "synthetic-pricing-only", 1_000_000, 1_000_000,
        ))
    return AgentsSDKInvestigatorRuntime(**kwargs)


def _admitted_projection(event):
    # Fixture-only metadata supplied by an external admission seam.
    # No parsing of event.original and no production authenticity claim.
    return IncidentProjection(
        event_sha256=event.digest,
        symptom="resource_pressure",
        severity="warning",
        observed_at="2030-01-02T11:30:00Z",
        target_role="ci_worker",
        observed_value=91,
        unit="percent",
    )


def _event():
    return SourceEvent(
        "synthetic-zabbix",
        b"private bytes never forwarded to model",
        "application/octet-stream",
        "synthetic-tenant",
        "synthetic-evt",
    )


def _investigator(runtime):
    connector = SimulatedConnector(
        "zabbix", {"read_history": {"cpu_pct": 83}},
    )
    store, audit = MemoryStore(), MemoryAudit()
    return Investigator(
        runtime,
        {"zabbix": connector},
        store,
        audit,
        Budget(3, 30),
        authorized_tenants=frozenset({"synthetic-tenant"}),
        allowed_operations={"zabbix": frozenset({"read_history"})},
    ), store, audit


def test_provider_is_disabled_by_default_without_network_or_credentials():
    investigator, store, audit = _investigator(
        _guarded_runtime(model="test-offline-model"),
    )
    result = investigator.investigate(_event())
    assert result.status == "failed"
    assert result.error_code == "RUNTIME_OR_CONNECTOR_FAILED"
    assert result.query_count == 0 and result.partial_evidence == ()
    assert store.records == [result] and audit.events[-1][2] == "failed"


def test_agents_sdk_tool_loop_with_mock_runner_never_uses_provider(monkeypatch):
    from agents import Runner
    from agents.tool_context import ToolContext

    def simulated_sdk_run(agent, input, max_turns):
        assert agent.name == "ANDY_OPS_INVESTIGATOR_V0"
        assert max_turns == 4
        assert len(agent.tools) == 1
        assert "private bytes" not in repr(input)
        assert "synthetic-tenant" not in repr(input)
        assert "resource_pressure" in repr(input)
        assert "observed_value" in repr(input)
        payload = json.dumps({"source": "zabbix", "operation": "read_history"})
        ctx = ToolContext(
            context=None,
            tool_name="read_observation",
            tool_call_id="synthetic-tool-call",
            tool_arguments=payload,
        )
        result = asyncio.run(agent.tools[0].on_invoke_tool(ctx, payload))
        assert json.loads(result)["observation"]["cpu_pct"] == 83
        assert "source_reference" not in json.loads(result)
        assert "event_sha256" not in json.loads(result)
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=21, output_tokens=13)), final_output=InvestigatorReport(
            hypotheses=[HypothesisReport(
                statement="Resource pressure is temporally compatible but not proven causal",
                evidence_indexes=[0],
                uncertainty="single synthetic data point",
            )],
            conclusion_summary="Resource pressure observed, causal link not proven",
            conclusion_evidence_indexes=[0],
            conclusion_classification="INDICATIVE",
        ))

    monkeypatch.setattr(Runner, "run_sync", staticmethod(simulated_sdk_run))
    investigator, store, audit = _investigator(_guarded_runtime(
        model="test-offline-model", enable_provider=True, projection_resolver=_admitted_projection, max_turns=4,
    ))
    result = investigator.investigate(_event())
    assert result.status == "completed"
    assert result.query_count == 1 and len(result.partial_evidence) == 1
    assert result.findings is not None
    assert result.findings.conclusion.evidence_indexes == (0,)
    assert result.findings.conclusion.classification == "INDICATIVE"
    assert result.findings.evidence == result.partial_evidence
    assert any(".observed:" in x[2] for x in audit.events)
    assert store.records == [result]


def test_agents_sdk_rejects_model_invented_evidence(monkeypatch):
    from agents import Runner

    monkeypatch.setattr(Runner, "run_sync", staticmethod(
        lambda *args, **kwargs: SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=21, output_tokens=13)), final_output=InvestigatorReport(
            conclusion_summary="Unsupported definitive cause",
            conclusion_evidence_indexes=[42],
            conclusion_classification="PROVEN",
        )),
    ))
    investigator, _, _ = _investigator(_guarded_runtime(
        model="test-offline-model", enable_provider=True, projection_resolver=_admitted_projection,
    ))
    result = investigator.investigate(_event())
    assert result.status == "failed"
    assert result.error_code == "INVALID_CONCLUSION_REFERENCE"
    assert result.partial_evidence == ()


def test_agents_sdk_has_no_unrestricted_tools_or_transport(monkeypatch):
    from agents import Runner

    def fake_run(agent, input, max_turns):
        assert all(tool.name == "read_observation" for tool in agent.tools)
        assert not agent.handoffs
        assert not agent.mcp_servers
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=21, output_tokens=13)), final_output=InvestigatorReport(
            conclusion_summary="Cannot determine cause",
            conclusion_evidence_indexes=[],
            conclusion_classification="UNKNOWN",
        ))

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_run))
    investigator, _, _ = _investigator(_guarded_runtime(
        model="test-offline-model", enable_provider=True, projection_resolver=_admitted_projection,
    ))
    result = investigator.investigate(_event())
    assert result.status == "completed"
    assert result.findings.conclusion is None
    assert result.findings.hypotheses[0].uncertainty.startswith("No observed evidence")


def test_sdk_tool_rejects_mutating_operation_without_connector_access(monkeypatch):
    import pytest
    from agents import Runner
    from agents.tool_context import ToolContext

    def fake_runner(agent, input, max_turns):
        payload = json.dumps({"source": "zabbix", "operation": "delete_event"})
        ctx = ToolContext(
            context=None,
            tool_name="read_observation",
            tool_call_id="attempt-write",
            tool_arguments=payload,
        )
        with pytest.raises(Exception):
            asyncio.run(agent.tools[0].on_invoke_tool(ctx, payload))
        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=21, output_tokens=13)), final_output=InvestigatorReport())

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_runner))
    investigator, _, audit = _investigator(_guarded_runtime(
        model="test-offline-model", enable_provider=True, projection_resolver=_admitted_projection,
    ))
    result = investigator.investigate(_event())
    assert result.query_count == 1
    assert result.partial_evidence == ()
    assert any(".denied:" in x[2] for x in audit.events)
    assert all("delete_event" not in x[2] for x in audit.events)


def test_malicious_tool_response_cannot_reach_sdk_context(monkeypatch):
    """The raw observation stays as restricted evidence, never a model-visible tool reply."""
    from agents import Runner
    from agents.tool_context import ToolContext

    secret = "token-do-not-copy-into-prompt"
    store, audit = MemoryStore(), MemoryAudit()
    connector = SimulatedConnector(
        "zabbix", {"read_history": {"cpu_pct": 83, "debug": secret}},
    )

    def model_stub(agent, input, max_turns):
        assert secret not in repr(input)
        payload = json.dumps({"source": "zabbix", "operation": "read_history"})
        ctx = ToolContext(
            context=None, tool_name="read_observation",
            tool_call_id="malicious-tool-output", tool_arguments=payload,
        )
        # The SDK's failure_error_function=None means this must raise, not
        # convert the rejected payload into a model-visible error string.
        return asyncio.run(agent.tools[0].on_invoke_tool(ctx, payload))

    monkeypatch.setattr(Runner, "run_sync", staticmethod(model_stub))
    investigator = Investigator(
        _guarded_runtime(
            model="offline-only", enable_provider=True,
            projection_resolver=_admitted_projection,
        ),
        {"zabbix": connector}, store, audit, Budget(2, 10),
        authorized_tenants=frozenset({"synthetic-tenant"}),
        allowed_operations={"zabbix": frozenset({"read_history"})},
    )
    result = investigator.investigate(_event())
    assert result.status == "failed"
    # A deliberately generic, non-sensitive code from the trusted view policy.
    assert result.error_code == "TOOL_OBSERVATION_REJECTED"
    assert result.query_count == 1
    assert len(result.partial_evidence) == 1
    assert result.partial_evidence[0].observation["debug"] == secret
    assert secret not in repr(audit.events)
    assert audit.events[-1][2] == "failed"
