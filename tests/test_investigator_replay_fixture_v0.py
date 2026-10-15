"""Contract tests for public synthetic replay data; no real runtimes/providers."""

import json
from pathlib import Path

import pytest

from attention_router.investigator.model import Query
from evals.investigator_v0.manifest import load_public_synthetic_case


CASE = Path(__file__).resolve().parents[1] / "evals/investigator_v0/synthetic_case_001.json"


def test_replay_exposes_seed_and_tools_but_no_investigation_path():
    event, connectors, allowed = load_public_synthetic_case(CASE)
    assert event.original.startswith(b"{")
    assert len(event.digest) == 64 and event.source_event_id == "synthetic-event-001"
    assert set(connectors) == {"zabbix", "tempo", "distributed_ci"}
    assert "read_history" in allowed["zabbix"]
    observation = connectors["zabbix"].read(Query("zabbix", "read_history"))
    assert len(observation.observation["series"]) == 3
    assert observation.source_reference.startswith("synthetic:")
    assert not hasattr(connectors["zabbix"], "write")


def test_replay_rejects_oracle_fields(tmp_path):
    obj = json.loads(CASE.read_text(encoding="utf-8"))
    obj["tools"]["tempo"]["read_trace"]["root_cause"] = "leaked answer"
    bad = tmp_path / "invalid.json"
    bad.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="REPLAY_ORACLE_EXPOSED"):
        load_public_synthetic_case(bad)


def test_replay_rejects_non_read_tool_operations(tmp_path):
    obj = json.loads(CASE.read_text(encoding="utf-8"))
    obj["tools"]["zabbix"]["delete_event"] = {}
    bad = tmp_path / "invalid.json"
    bad.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="REPLAY_OPERATION_NOT_READ_ONLY"):
        load_public_synthetic_case(bad)


def test_replay_rejects_non_synthetic_identity(tmp_path):
    obj = json.loads(CASE.read_text(encoding="utf-8"))
    obj["seed"]["tenant_id"] = "private-tenant"
    bad = tmp_path / "invalid.json"
    bad.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="REPLAY_TENANT_NOT_SYNTHETIC"):
        load_public_synthetic_case(bad)
