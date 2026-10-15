"""Offline integration checks for the pinned Agents SDK public function-tool API."""

import asyncio
import json
from types import SimpleNamespace

from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime, HypothesisReport, InvestigatorReport,
)
from attention_router.investigator.model import SourceEvent
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import MemoryAudit, MemoryStore, SimulatedConnector


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
        AgentsSDKInvestigatorRuntime(model="test-offline-model"),
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
        payload = json.dumps({"source": "zabbix", "operation": "read_history"})
        ctx = ToolContext(
            context=None,
            tool_name="read_observation",
            tool_call_id="synthetic-tool-call",
            tool_arguments=payload,
        )
        result = asyncio.run(agent.tools[0].on_invoke_tool(ctx, payload))
        assert json.loads(result)["observation"]["cpu_pct"] == 83
        return SimpleNamespace(final_output=InvestigatorReport(
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
    investigator, store, audit = _investigator(AgentsSDKInvestigatorRuntime(
        model="test-offline-model", enable_provider=True, max_turns=4,
    ))
    result = investigator.investigate(_event())
    assert result.status == "completed"
    assert result.query_count == 1 and len(result.partial_evidence) == 1
    assert result.findings is not None
    assert result.findings.conclusion.evidence_indexes == (0,)
    assert result.findings.evidence == result.partial_evidence
    assert any(".observed:" in x[2] for x in audit.events)
    assert store.records == [result]


def test_agents_sdk_rejects_model_invented_evidence(monkeypatch):
    from agents import Runner

    monkeypatch.setattr(Runner, "run_sync", staticmethod(
        lambda *args, **kwargs: SimpleNamespace(final_output=InvestigatorReport(
            conclusion_summary="Unsupported definitive cause",
            conclusion_evidence_indexes=[42],
            conclusion_classification="PROVEN",
        )),
    ))
    investigator, _, _ = _investigator(AgentsSDKInvestigatorRuntime(
        model="test-offline-model", enable_provider=True,
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
        return SimpleNamespace(final_output=InvestigatorReport(
            conclusion_summary="Cannot determine cause",
            conclusion_evidence_indexes=[],
            conclusion_classification="UNKNOWN",
        ))

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_run))
    investigator, _, _ = _investigator(AgentsSDKInvestigatorRuntime(
        model="test-offline-model", enable_provider=True,
    ))
    result = investigator.investigate(_event())
    assert result.status == "completed"
    assert result.findings.conclusion is None
    assert result.findings.hypotheses[0].uncertainty.startswith("No observed evidence")
