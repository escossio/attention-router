"""Byte-budget tests exercise the broker without a model, network or real sources."""

import pytest

from attention_router.investigator.model import Query, SourceEvent
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import (
    MemoryAudit, MemoryStore, ScriptedRuntime, SimulatedConnector,
)


def _run(queries, responses, budget):
    connectors = {
        name: SimulatedConnector(name, {"inspect": response})
        for name, response in responses.items()
    }
    audit, store = MemoryAudit(), MemoryStore()
    investigator = Investigator(
        ScriptedRuntime(tuple(queries)),
        connectors, store, audit, budget,
        authorized_tenants=frozenset({"tenant-test"}),
        allowed_operations={name: frozenset({"inspect"}) for name in responses},
    )
    result = investigator.investigate(
        SourceEvent("test", b"opaque seed", "application/octet-stream", "tenant-test"),
    )
    return result, store, audit


def test_per_query_volume_budget_fails_closed_without_retaining_oversized_evidence():
    result, store, audit = _run(
        (Query("zabbix", "inspect"),),
        {"zabbix": {"data": "synthetic-" * 60}},
        Budget(2, 10, max_evidence_bytes=120, max_total_evidence_bytes=4096),
    )
    assert result.status == "limited"
    assert result.error_code == "EVIDENCE_BYTES_PER_QUERY_EXCEEDED"
    assert result.query_count == 1
    assert result.partial_evidence == () and result.findings is None
    assert store.records == [result]
    assert any(".limited:" in item[2] for item in audit.events)
    assert audit.events[-1][2] == "limited"
    assert "synthetic-" not in repr(audit.events)


def test_cumulative_budget_preserves_first_observation_and_limits_second():
    result, _, audit = _run(
        (Query("zabbix", "inspect"), Query("tempo", "inspect")),
        {"zabbix": {"state": "synthetic"}, "tempo": {"state": "synthetic"}},
        Budget(2, 10, max_evidence_bytes=4096, max_total_evidence_bytes=125),
    )
    assert result.status == "limited"
    assert result.error_code == "EVIDENCE_BYTES_TOTAL_EXCEEDED"
    assert result.query_count == 2
    assert len(result.partial_evidence) == 1
    assert result.partial_evidence[0].source_reference == "synthetic:zabbix:inspect"
    assert any("query.2.limited:" in item[2] for item in audit.events)
    assert sum(".observed:" in item[2] for item in audit.events) == 1


def test_unsupported_evidence_encoding_is_a_generic_failure_not_a_payload_leak():
    result, _, audit = _run(
        (Query("zabbix", "inspect"),),
        {"zabbix": {"unserializable": {"private-non-json"}}},
        Budget(2, 10),
    )
    assert result.status == "failed"
    assert result.error_code == "EVIDENCE_ENCODING_INVALID"
    assert result.partial_evidence == ()
    assert any(".failed:" in item[2] for item in audit.events)
    assert "private-non-json" not in repr(audit.events)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_evidence_bytes": 0},
        {"max_total_evidence_bytes": -1},
        {"max_evidence_bytes": True},
        {"max_total_evidence_bytes": 1.5},
    ],
)
def test_invalid_byte_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_BUDGET"):
        Budget(3, 10, **kwargs)


def test_small_tool_observation_still_completes_normally():
    result, store, _ = _run(
        (Query("zabbix", "inspect"),),
        {"zabbix": {"state": "synthetic"}},
        Budget(2, 10, max_evidence_bytes=120, max_total_evidence_bytes=125),
    )
    assert result.status == "completed"
    assert result.error_code is None
    assert len(result.partial_evidence) == 1
    assert result.findings.evidence == result.partial_evidence
    assert store.records == [result]
