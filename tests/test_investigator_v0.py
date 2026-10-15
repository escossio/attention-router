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
    return Investigator(
        runtime, connectors, store, audit, Budget(limit, 5),
        authorized_tenants=frozenset({"tenant-test"}),
        allowed_operations={source: frozenset({"inspect"}) for source in SOURCES},
    ), store, audit


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


def test_denied_operation_is_audited_without_disclosing_arguments():
    sensitive = "never-echo-private-argument"
    runtime = ScriptedRuntime((Query("linux", "kill", {"token": sensitive}),))
    investigator, store, audit = _fixture(runtime)
    result = investigator.investigate(SourceEvent("test", b"opaque", "text/plain", "tenant-test"))
    assert result.status == "blocked"
    assert result.error_code == "OPERATION_NOT_ALLOWED"
    assert result.partial_evidence == () and result.query_count == 1
    assert any(".denied:" in item[2] for item in audit.events)
    assert sensitive not in repr(audit.events)
    assert store.records == [result]


def test_unauthorized_tenant_fails_closed_before_runtime_is_invoked():
    runtime = ScriptedRuntime((Query("zabbix", "inspect"),))
    investigator, store, audit = _fixture(runtime)
    result = investigator.investigate(
        SourceEvent("test", b"untrusted", "text/plain", "tenant-not-authorized"),
    )
    assert result.status == "blocked" and result.error_code == "TENANT_NOT_AUTHORIZED"
    assert result.query_count == 0 and result.partial_evidence == ()
    assert store.records == [result] and audit.events[-1][2] == "blocked"


def test_failure_preserves_observed_evidence_with_per_query_audit():
    runtime = ScriptedRuntime((Query("zabbix", "inspect"), Query("linux", "inspect")))
    investigator, _, audit = _fixture(runtime, fail_source="linux")
    result = investigator.investigate(SourceEvent("test", b"source", "text/plain", "tenant-test"))
    assert result.status == "failed" and result.findings is None
    assert len(result.partial_evidence) == 1
    assert result.partial_evidence[0].source_reference == "synthetic:zabbix:inspect"
    assert result.query_count == 2
    events = [entry[2] for entry in audit.events]
    assert sum(".requested:" in entry for entry in events) == 2
    assert any(".observed:" in entry for entry in events)
    assert any(".failed:" in entry for entry in events)


def test_budget_still_keeps_first_observation_for_replay():
    runtime = ScriptedRuntime((Query("zabbix", "inspect"), Query("tempo", "inspect")))
    investigator, _, _ = _fixture(runtime, limit=1)
    result = investigator.investigate(SourceEvent("test", b"raw", "text/plain", "tenant-test"))
    assert result.status == "limited"
    assert result.error_code == "QUERY_BUDGET_EXCEEDED"
    assert len(result.partial_evidence) == 1
    assert result.partial_evidence[0].source_reference == "synthetic:zabbix:inspect"


def test_mutating_tool_response_does_not_overwrite_retained_evidence():
    class MutatingRuntime:
        def investigate(self, event, queries):
            evidence = queries.read(Query("zabbix", "inspect"))
            evidence.observation["state"] = "fabricated"
            return Findings((evidence,), (), None)

    investigator, _, _ = _fixture(MutatingRuntime())
    result = investigator.investigate(SourceEvent("test", b"original", "text/plain", "tenant-test"))
    assert result.status == "failed" and result.error_code == "UNOBSERVED_EVIDENCE"
    assert result.partial_evidence[0].observation["state"] == "synthetic"


def test_no_ambient_grants_means_deny_by_default():
    connector = SimulatedConnector("zabbix", {"inspect": {"state": "synthetic"}})
    store, audit = MemoryStore(), MemoryAudit()
    investigator = Investigator(
        ScriptedRuntime((Query("zabbix", "inspect"),)),
        {"zabbix": connector}, store, audit, Budget(2, 5),
    )
    result = investigator.investigate(SourceEvent("test", b"raw", "text/plain", "tenant-test"))
    assert result.status == "blocked"
    assert result.error_code == "TENANT_NOT_AUTHORIZED"
    assert result.query_count == 0
