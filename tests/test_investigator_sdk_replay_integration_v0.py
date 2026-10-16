"""Prove replay-to-SDK-to-query-to-evidence plumbing without any model request.

A mocked Runner selects its second read from the first tool result. This proves
composition and auditability, NOT autonomous reasoning or incident causality.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from attention_router.investigator.model_usage_gate import (
    ModelLimits, ModelUsage, ModelUsageGate,
)
from attention_router.investigator.agents_sdk_runtime import (
    AgentsSDKInvestigatorRuntime,
    HypothesisReport,
    InvestigatorReport, ModelPricing,
)
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.projection import IncidentProjection
from attention_router.investigator.simulated import MemoryAudit, MemoryStore
from evals.investigator_v0.manifest import load_public_synthetic_case
from evals.investigator_v0.structural_grader import assess_replay_structure


def _guarded_runtime(**kwargs):
    """Offline SDK test keeps pricing and reservation independent of the Runner."""
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


CASE = (
    Path(__file__).resolve().parents[1]
    / "evals/investigator_v0/synthetic_case_001.json"
)


def test_mocked_agents_sdk_queries_replay_on_demand_and_preserves_evidence(monkeypatch):
    from agents import Runner
    from agents.tool_context import ToolContext

    event, connectors, grants = load_public_synthetic_case(CASE)
    store, audit = MemoryStore(), MemoryAudit()
    def admitted_projection(incident):
        # Admission fixture chosen independently of incident.original bytes.
        return IncidentProjection(
            event_sha256=incident.digest,
            symptom="resource_pressure",
            severity="warning",
            observed_at="2030-01-02T11:30:00Z",
            target_role="ci_worker",
            observed_value=91,
            unit="percent",
        )

    runtime = _guarded_runtime(
        model="offline-stub-only",
        enable_provider=True,
        max_turns=6,
        projection_resolver=admitted_projection,
    )

    def fake_run(agent, input, max_turns):
        assert max_turns == 6
        assert len(agent.tools) == 1 and agent.tools[0].name == "read_observation"
        assert not agent.handoffs and not agent.mcp_servers
        # Event bytes, original alert fields and tenant must remain out of prompt.
        prompt = json.dumps(input)
        assert "resource-pressure" not in prompt
        assert "resource_pressure" in prompt
        assert "synthetic-event-001" not in prompt
        assert "synthetic-tenant" not in prompt

        def consult(source, operation, index):
            payload = json.dumps({"source": source, "operation": operation})
            context = ToolContext(
                context=None,
                tool_name="read_observation",
                tool_call_id=f"replay-call-{index}",
                tool_arguments=payload,
            )
            return json.loads(asyncio.run(agent.tools[0].on_invoke_tool(context, payload)))

        first = consult("zabbix", "read_event", 0)
        assert first["evidence_index"] == 0
        assert first["observation"]["severity"] == "warning"

        # Follow-up queries depend on tool observations, not a hardcoded cause.
        second_op = "read_history" if first["observation"]["severity"] == "warning" else "read_event"
        second = consult("zabbix", second_op, 1)
        assert second["evidence_index"] == 1
        assert len(second["observation"]["series"]) == 3

        third = consult("distributed_ci", "read_jobs", 2)
        assert third["evidence_index"] == 2
        assert third["observation"]["jobs"][0]["worker"] == "synthetic-node"

        return SimpleNamespace(context_wrapper=SimpleNamespace(usage=SimpleNamespace(requests=1, input_tokens=21, output_tokens=13)), final_output=InvestigatorReport(
            hypotheses=[
                HypothesisReport(
                    statement="Observed resource pressure could align with CI activity",
                    evidence_indexes=[0, 1, 2],
                    uncertainty="No causal isolation or counterfactual observation",
                    status="UNRESOLVED",
                ),
            ],
            conclusion_summary="Alert and job overlap, but root cause remains unverified",
            conclusion_evidence_indexes=[0, 1, 2],
            conclusion_classification="INDICATIVE",
            limitations=["Temporal alignment alone does not prove cause"],
        ))

    monkeypatch.setattr(Runner, "run_sync", staticmethod(fake_run))
    investigator = Investigator(
        runtime, connectors, store, audit, Budget(6, 30),
        authorized_tenants=frozenset({event.tenant_id}),
        allowed_operations=grants,
    )
    result = investigator.investigate(event)
    assert result.status == "completed", result.error_code
    assert result.query_count == 3
    assert len(result.partial_evidence) == 3
    assert result.findings.evidence == result.partial_evidence
    assert result.findings.conclusion.classification == "INDICATIVE"
    assert result.findings.hypotheses[0].status == "UNRESOLVED"
    assert result.event.original == event.original
    assert result.event.digest == event.digest
    assert len([a for a in audit.events if ".requested:" in a[2]]) == 3
    assert len([a for a in audit.events if ".observed:" in a[2]]) == 3
    assert store.records == [result]
    assessment = assess_replay_structure(
        result, audit.events, expected_seed_sha256=event.digest, max_queries=6,
    )
    assert assessment.passed, assessment.checks
    assert assessment.causal_proof == "NOT_EVALUATED"


def test_real_provider_stays_disabled_in_replay_without_mock_or_key(monkeypatch):
    event, connectors, grants = load_public_synthetic_case(CASE)
    # No monkeypatch of Runner here; the default must stop before importing
    # provider/model code or performing any external call.
    store, audit = MemoryStore(), MemoryAudit()
    runtime = _guarded_runtime(model="offline-never-call-provider")
    investigator = Investigator(
        runtime, connectors, store, audit, Budget(4, 2),
        authorized_tenants=frozenset({event.tenant_id}),
        allowed_operations=grants,
    )
    result = investigator.investigate(event)
    assert result.status == "failed"
    assert result.error_code == "RUNTIME_OR_CONNECTOR_FAILED"
    assert result.query_count == 0
    assert result.partial_evidence == ()
    assert audit.events[-1][2] == "failed"
