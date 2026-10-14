"""Synthetic evidence only: no private inventory, bodies, or live network access."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "flow_map", ROOT / "ops/observability/grafana/flow/flow_map.py"
)
flow_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow_map)

NOW = 1_800_000_000
SERVICES = {
    "attention-router-transport": "runtime/transport/1",
    "attention-router-ingress": "runtime/ingress/1",
    "attention-router-worker": "runtime/worker/1",
}
TID = "1" * 32
INBOUND_TID = "2" * 32


def sample(service, clock=NOW - 60, state="running", health="healthy"):
    return {
        "clock": clock,
        "value": {
            "entity_key": SERVICES[service],
            "state": state, "health": health, "name": "container-" + service,
            "host_name": "host-a",
            "environment": "DO_NOT_EXPORT",
            "private_body": "MUST_NOT_APPEAR",
        },
    }


def attachment(service, ip, clock=NOW - 60):
    return {
        "clock": clock,
        "value": {
            "entity_key": SERVICES[service], "network": "vlan-test",
            "ip": ip, "mac": "12:34:56:78:90:12",
        },
    }


def result(samples, **kwargs):
    return flow_map.build_flow_map(
        service_entities=SERVICES, container_samples=samples, now_s=NOW, **kwargs,
    )


def test_nodes_use_logical_identity_and_fresh_zabbix_health():
    data = result([sample(s) for s in SERVICES], attachment_samples=[
        attachment("attention-router-transport", "10.1.1.2")
    ])
    assert [n["id"] for n in data["nodes"]] == list(SERVICES)
    assert all(n["health"]["status"] == "HEALTHY" for n in data["nodes"])
    assert data["nodes"][0]["interfaces"] == [{"ip": "10.1.1.2", "network": "vlan-test"}]
    assert data["edges"] == []
    assert "MUST_NOT_APPEAR" not in str(data)
    assert "DO_NOT_EXPORT" not in str(data)


def test_stale_health_and_ip_never_become_healthy_or_live_interface():
    data = result(
        [sample("attention-router-transport", clock=NOW-220)],
        attachment_samples=[attachment("attention-router-transport", "10.1.1.2", clock=NOW-220)],
        legacy_alerts=[{"entity_key": SERVICES["attention-router-transport"], "active": True}],
    )
    transport = data["nodes"][0]
    assert transport["mainStat"] == "UNKNOWN"
    assert transport["health"]["reason"] == "STALE_EVIDENCE"
    assert transport["health"]["last_observed_health"] == "healthy"
    assert transport["interfaces"] == []
    assert not data["divergences"]
    assert data["nodes"][1]["health"]["reason"] == "MISSING_EVIDENCE"
    assert all(n["color"] == "yellow" for n in data["nodes"])


def test_legacy_trigger_conflict_does_not_override_fresh_container_health():
    data = result(
        [sample("attention-router-transport")],
        legacy_alerts=[{"entity_key": SERVICES["attention-router-transport"], "active": True}],
    )
    assert data["nodes"][0]["mainStat"] == "HEALTHY"
    assert data["divergences"][0]["kind"] == "LEGACY_HEALTH_CONFLICT"


def test_explicit_errors_and_unknown_without_healthcheck_are_not_fabricated():
    data = result([
        sample("attention-router-transport", health="unhealthy"),
        sample("attention-router-ingress", state="exited", health=""),
        sample("attention-router-worker", health=""),
    ])
    assert [n["mainStat"] for n in data["nodes"]] == ["DOWN", "DOWN", "UNKNOWN"]


def test_otels_span_links_not_confused_with_parent_child_or_stages():
    canonical = {"name": "attention.message", "service": "attention-router-ingress",
                 "trace_id": TID, "span_id": "a" * 16, "parent_span_id": "",
                 "duration_ms": 4, "status": "OK"}
    worker = {"name": "worker.dispatch", "service": "attention-router-worker",
              "trace_id": TID, "span_id": "b" * 16, "parent_span_id": "a" * 16,
              "duration_ms": 2, "status": "UNSET"}
    inbound = {"name": "transport.ingress_attempt", "service": "attention-router-transport",
               "trace_id": INBOUND_TID, "span_id": "c" * 16, "parent_span_id": "",
               "duration_ms": 3, "status": "OK"}
    trace = {"latency": [canonical, worker, inbound],
             "metadata": [{
                 "trace_id": TID, "relationship": "SPAN_LINK", "link_target_status": "VERIFIED",
                 "inbound_trace_id": INBOUND_TID, "inbound_span_id": "c" * 16,
             }]}
    data = result(
        [sample(s) for s in SERVICES],
        expected_edges=[("attention-router-transport", "attention-router-ingress")],
        trace_view=trace,
    )
    relationships = {e["relationship"] for e in data["edges"]}
    assert relationships == {"EXPECTED_TOPOLOGY", "PARENT_CHILD", "SPAN_LINK"}
    assert all(n["secondaryStat"] == "REACHED" for n in data["nodes"])
    assert len(data["nodes"]) == 3  # Decision stage is not an invented container.
    assert data["trace_id"] == TID


def test_network_witness_does_not_claim_selected_trace_correlation():
    data = result(
        [sample(s) for s in SERVICES],
        attachment_samples=[
            attachment("attention-router-transport", "10.1.1.2"),
            attachment("attention-router-ingress", "10.1.1.10"),
        ],
        network_events=[{
            "capture_timestamp_ns": (NOW-10) * 1_000_000_000,
            "src_ip": "10.1.1.2", "src_port": 44000,
            "dst_ip": "10.1.1.10", "dst_port": 18083,
            "vlan_id": 212, "payload": "DO_NOT_EXPOSE",
        }],
        network_sensor_healthy=False,
    )
    witness = data["network_evidence"][0]
    assert witness["trace_correlation"] == "UNVERIFIED"
    assert witness["sensor_quality"] == "PARTIAL_SENSOR"
    assert witness["protocol"] == "TCP"
    assert "DO_NOT_EXPOSE" not in str(data)


def test_stale_network_and_unmapped_edges_are_not_drawn():
    data = result(
        [sample(s) for s in SERVICES],
        attachment_samples=[
            attachment("attention-router-transport", "10.1.1.2"),
            attachment("attention-router-ingress", "10.1.1.10"),
        ],
        expected_edges=[("attention-router-transport", "nonexistent")],
        network_events=[{
            "capture_timestamp_ns": (NOW-300)*1_000_000_000,
            "src_ip": "10.1.1.2", "src_port": 1,
            "dst_ip": "10.1.1.10", "dst_port": 2,
        }],
    )
    assert data["edges"] == []
    assert data["network_evidence"] == []


def test_reject_alias_collision_and_oversized_inputs():
    try:
        flow_map.build_flow_map(
            service_entities={"one": "duplicate", "two": "duplicate"},
            container_samples=[], now_s=NOW)
        raise AssertionError("collision accepted")
    except ValueError:
        pass
    try:
        result([sample("attention-router-transport")] * 257)
        raise AssertionError("oversized inventory accepted")
    except ValueError:
        pass


def test_overlapping_span_ids_in_different_traces_do_not_cross_reparent():
    root = {"name": "attention.message", "service": "attention-router-ingress",
            "trace_id": TID, "span_id": "a" * 16, "parent_span_id": ""}
    worker = {"name": "worker.dispatch", "service": "attention-router-worker",
              "trace_id": TID, "span_id": "b" * 16, "parent_span_id": "a" * 16}
    unrelated = {"name": "transport.ingress_attempt", "service": "attention-router-transport",
                 "trace_id": INBOUND_TID, "span_id": "a" * 16, "parent_span_id": ""}
    trace = {"metadata": [{"trace_id": TID}], "latency": [root, worker, unrelated]}
    data = result([sample(s) for s in SERVICES], trace_view=trace)
    observed = [e for e in data["edges"] if e["relationship"] == "PARENT_CHILD"]
    assert len(observed) == 1
    assert observed[0]["source"] == "attention-router-ingress"
    assert observed[0]["target"] == "attention-router-worker"
