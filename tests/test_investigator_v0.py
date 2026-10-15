"""Offline unit, port-contract, and simulated integration checks."""

from attention_router.investigator.model import Evidence, Findings, Query, SourceEvent
from attention_router.investigator.service import Budget, Investigator
from attention_router.investigator.simulated import (
    SOURCES, MemoryAudit, MemoryStore, ScriptedRuntime, SimulatedConnector,
)


def _fixture(runtime, *, limit=6, fail_source=None):
    connectors = {
        source: SimulatedConnector(source, {"inspect": {"state": "synthetic"}},
                                   fail=source == fail_source)
        for source in SOURCES
    }
    store, audit = MemoryStore(), MemoryAudit()
    return Investigator(runtime, connectors, store, audit, Budget(limit, 5)), store, audit


def test_full_simulated_flow_preserves_original_and_correlation():
    queries = tuple(Query(source, "inspect") for source in SOURCES)
    investigator, store, audit = _fixture(ScriptedRuntime(queries))
    original = b'\x00{"alert":"opaque original"}\xff'
    event = SourceEvent("synthetic-alert", original, "application/octet-stream", "tenant-test", "evt-1")
    result = investigator.investigate(event)
    assert result.status == "completed" and result.query_count == len(SOURCES)
    assert result.event.original == original and result.event.digest == event.digest
    assert store.records == [result]
    assert {entry[0] for entry in audit.events} == {result.investigation_id}
    assert {entry[1] for entry in audit.events} == {event.correlation_id}
    assert original.decode("latin1") not in repr(audit.events)
    assert result.findings.hypotheses[0].statement != result.findings.evidence[0].observation
    assert result.findings.conclusion.evidence_indexes == tuple(range(len(SOURCES)))


def test_budget_stops_before_excess_query():
    runtime = ScriptedRuntime((Query("zabbix", "inspect"), Query("tempo", "inspect")))
    investigator, store, audit = _fixture(runtime, limit=1)
    result = investigator.investigate(SourceEvent("test", b"raw", "text/plain", "tenant-test"))
    assert (result.status, result.error_code, result.query_count) == (
        "limited", "QUERY_BUDGET_EXCEEDED", 1,
    )
    assert result.findings is None and store.records == [result]
    assert audit.events[-1][2] == "limited"


def test_connector_failure_is_opaque_and_audited():
    investigator, store, audit = _fixture(
        ScriptedRuntime((Query("linux", "inspect"),)), fail_source="linux",
    )
    result = investigator.investigate(SourceEvent("test", b"secret fixture", "text/plain", "tenant-test"))
    assert result.status == "failed"
    assert result.error_code == "RUNTIME_OR_CONNECTOR_FAILED"
    assert result.query_count == 1 and result.findings is None
    assert store.records == [result] and audit.events[-1][2] == "failed"
    assert "secret fixture" not in repr(audit.events)


def test_fake_evidence_rejected():
    class FabricatingRuntime:
        def investigate(self, event, queries):
            query = Query("zabbix", "inspect")
            queries.read(query)
            return Findings((Evidence(query, {"state": "invented"}, "fake"),), (), None)

    investigator, _, _ = _fixture(FabricatingRuntime())
    result = investigator.investigate(SourceEvent("test", b"raw", "text/plain", "tenant-test"))
    assert (result.status, result.error_code) == ("failed", "UNOBSERVED_EVIDENCE")


def test_simulated_connector_contract_is_read_only():
    connector = SimulatedConnector("postgresql", {"inspect": {"rows": 2}})
    evidence = connector.read(Query("postgresql", "inspect"))
    assert evidence.observation == {"rows": 2}
    assert evidence.source_reference.startswith("synthetic:")
    assert not hasattr(connector, "write")


def test_time_budget_blocks_runtime(monkeypatch):
    from attention_router.investigator import service

    ticks = iter((0.0, 5.0))
    monkeypatch.setattr(service, "monotonic", lambda: next(ticks))
    investigator, _, _ = _fixture(ScriptedRuntime(()))
    result = investigator.investigate(SourceEvent("test", b"raw", "text/plain", "tenant-test"))
    assert (result.status, result.error_code) == ("limited", "TIME_BUDGET_EXCEEDED")
