"""Offline contract tests for the real one-shot boundary; no provider calls."""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

from attention_router.investigator.model_usage_gate import ModelUsage
from attention_router.investigator.process_supervisor import run_with_deadline
from ops.investigator import real_once as one


def _paths(tmp_path):
    return tmp_path / "result.json", tmp_path / "journal.jsonl"


def test_missing_key_blocks_before_supervisor(monkeypatch, tmp_path):
    result, journal = _paths(tmp_path)
    monkeypatch.setattr(one, "run_with_deadline", lambda *a, **k: pytest.fail("model child started"))
    assert one._parent(tmp_path / "absent", result, journal) == 2
    assert json.loads(result.read_text())["status"] == "blocked"
    assert not journal.exists()


def test_invalid_quote_and_admission_block_before_supervisor(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    monkeypatch.setattr(one, "run_with_deadline", lambda *a, **k: pytest.fail("model child started"))
    monkeypatch.setattr(one, "QUOTE", ModelUsage(4000, 2000, 0))
    result, journal = _paths(tmp_path)
    assert one._parent(key, result, journal) == 2
    result.unlink()
    monkeypatch.setattr(one, "QUOTE", ModelUsage(4000, 2000, 3200))
    monkeypatch.setattr(one, "_admit", lambda: (_ for _ in ()).throw(ValueError("invalid")))
    assert one._parent(key, result, journal) == 2


def test_parent_invokes_hard_supervisor_and_timeout_is_not_a_conclusion(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    result, journal = _paths(tmp_path)
    seen = {}

    def timed_out(argv, *, deadline_seconds, env, cwd):
        seen.update(argv=argv, deadline=deadline_seconds, env=env, cwd=cwd)
        return SimpleNamespace(timed_out=True, exit_code=None)

    monkeypatch.setattr(one, "run_with_deadline", timed_out)
    assert one._parent(key, result, journal) == 124
    assert seen["deadline"] == 75
    assert seen["argv"][:3] == [sys.executable, "-m", "ops.investigator.real_once"]
    assert "OPENAI_API_KEY" not in seen["env"]
    assert json.loads(result.read_text()) == {
        "run_id": json.loads(result.read_text())["run_id"], "status": "timeout",
        "query_count": 0, "usage": None, "usage_state": "unknown",
        "conclusion": None, "limited": True,
    }
    assert '"event":"timeout"' in journal.read_text()
    assert "synthetic-test-key" not in journal.read_text()


def test_supervisor_really_kills_isolated_child_on_deadline():
    outcome = run_with_deadline(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        deadline_seconds=0.05, env={"PATH": "/usr/bin:/bin"},
    )
    assert outcome.timed_out and outcome.exit_code is None


@pytest.mark.parametrize("tokens,status,limited", [(0, "failed", True), (21, "completed", False)])
def test_child_synthetic_tool_call_journal_and_usage(monkeypatch, tmp_path, tokens, status, limited):
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    result, journal = _paths(tmp_path)
    fake = ModuleType("agents")
    fake.set_default_openai_client = lambda client, use_for_tracing: None
    openai = ModuleType("openai")
    openai.AsyncOpenAI = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "openai", openai)
    fake.set_tracing_disabled = lambda value: None
    fake.Agent = lambda **kwargs: SimpleNamespace(**kwargs)
    fake.AgentHooks = object
    fake.AgentOutputSchema = lambda *args, **kwargs: None
    fake.ModelSettings = lambda **kwargs: SimpleNamespace(**kwargs)
    fake.function_tool = lambda **kwargs: (lambda function: SimpleNamespace(function=function))

    def run_sync(agent, input, max_turns):
        from attention_router.investigator.agents_sdk_runtime import InvestigatorReport

        assert max_turns == 4 and agent.model == one.MODEL
        assert agent.model_settings.max_tokens == 500
        assert agent.model_settings.parallel_tool_calls is False
        observation = agent.tools[0].function("zabbix", "read_event")
        assert json.loads(observation)["observation"]["severity"] == "warning"
        assert "synthetic-test-key" not in repr(input)
        report = InvestigatorReport(
            conclusion_summary="Resource pressure remains unproven",
            conclusion_evidence_indexes=[0],
            conclusion_classification="INDICATIVE",
            limitations=["Single synthetic observation"],
        )
        return SimpleNamespace(
            context_wrapper=SimpleNamespace(usage=SimpleNamespace(
                requests=1, input_tokens=tokens, output_tokens=tokens,
            )), final_output=report,
        )

    fake.Runner = SimpleNamespace(run_sync=run_sync)
    monkeypatch.setitem(sys.modules, "agents", fake)
    run_id = str(uuid4())
    assert one._child(key, result, journal, run_id) == one.EXIT[status]
    summary = json.loads(result.read_text())
    expected_usage = None if tokens == 0 else {
        "input_tokens": tokens, "output_tokens": tokens,
        "cost_micro_usd": one.PRICING.cost_micro_usd(tokens, tokens),
    }
    assert summary["run_id"] == run_id and summary["status"] == status
    assert summary["query_count"] == 1 and summary["usage"] == expected_usage
    assert summary["usage_state"] == ("unknown" if tokens == 0 else "complete")
    assert summary["limited"] is limited
    if tokens:
        assert summary["conclusion"] == {
            "classification": "INDICATIVE",
            "summary": "Resource pressure remains unproven",
            "summary_redacted": False,
            "limitations": ["Single synthetic observation"],
            "limitations_truncated": False,
            "evidence_refs": [{"index": 0, "source": "zabbix", "operation": "read_event"}],
        }
    else:
        assert summary["conclusion"] is None
    events = [json.loads(line)["event"] for line in journal.read_text().splitlines()]
    assert events == ["started", "query_requested", "query_observed", status]
    assert "synthetic-test-key" not in journal.read_text() + result.read_text()
    assert result.stat().st_mode & 0o777 == 0o600


def test_private_conclusion_redacts_sensitive_text_and_never_saves_payload():
    from attention_router.investigator.model import (
        Conclusion, Evidence, Findings, Investigation, Query, SourceEvent,
    )

    secret = "sk-proj-ABCDEF123456789"
    query = Query("zabbix", "read_event", {"token": secret})
    evidence = Evidence(query, {"debug": secret}, "synthetic:zabbix:read_event")
    findings = Findings((evidence,), (), Conclusion(
        secret, (0,), ("Only one sample", "https://private.example/secret"), "UNKNOWN",
    ))
    result = Investigation(str(uuid4()), SourceEvent(
        "synthetic-zabbix", b"seed", "text/plain", "synthetic-tenant",
    ), findings, "completed", 1, partial_evidence=(evidence,))
    view = one._safe_conclusion(result, {"zabbix": frozenset({"read_event"})})
    assert view["classification"] == "UNKNOWN"
    assert view["summary"] is None and view["summary_redacted"] is True
    assert view["limitations"] == ["Only one sample", "REDACTED"]
    assert view["evidence_refs"] == [{
        "index": 0, "source": "zabbix", "operation": "read_event",
    }]
    assert secret not in json.dumps(view) and "private.example" not in json.dumps(view)


def test_child_persists_known_partial_usage_after_runner_error(monkeypatch, tmp_path):
    from agents import Runner

    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    result, journal = _paths(tmp_path)

    def failed_after_response(agent, input, max_turns):
        context = SimpleNamespace(usage=SimpleNamespace(
            requests=1, input_tokens=40, output_tokens=15,
        ))
        asyncio.run(agent.hooks.on_llm_end(context, agent, None))
        raise RuntimeError("synthetic tool failure")

    monkeypatch.setattr(Runner, "run_sync", staticmethod(failed_after_response))
    assert one._child(key, result, journal, str(uuid4())) == one.EXIT["failed"]
    summary = json.loads(result.read_text())
    assert summary["usage_state"] == "partial"
    assert summary["usage"] == {
        "input_tokens": 40, "output_tokens": 15,
        "cost_micro_usd": one.PRICING.cost_micro_usd(40, 15),
    }
    assert summary["status"] == "failed" and summary["limited"] is True
    assert summary["conclusion"] is None
    assert "synthetic-test-key" not in result.read_text() + journal.read_text()

def test_supervised_run_id_rejects_untrusted_non_uuid4():
    from attention_router.investigator.model import SourceEvent
    from attention_router.investigator.service import Budget, Investigator
    from attention_router.investigator.simulated import (
        MemoryAudit, MemoryStore, ScriptedRuntime,
    )

    investigator = Investigator(
        ScriptedRuntime(()), {}, MemoryStore(), MemoryAudit(), Budget(1, 5),
        authorized_tenants=frozenset({"synthetic"}),
    )
    event = SourceEvent("synthetic", b"seed", "text/plain", "synthetic")
    with pytest.raises(ValueError, match="INVALID_INVESTIGATION_ID"):
        investigator.investigate(event, investigation_id="untrusted-id")
    assert investigator.investigate(event, investigation_id=str(uuid4())).status == "completed"
