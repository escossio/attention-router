import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "flow_reader", ROOT / "ops/observability/grafana/flow/reader.py"
)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def trace(spans):
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "attention-router-worker"}}
                    ]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def span(name, sid, parent="", outcome="BLOCKED", links=()):
    return {
        "name": name,
        "traceId": "1" * 32,
        "spanId": sid,
        "parentSpanId": parent,
        "startTimeUnixNano": "1000000",
        "endTimeUnixNano": "2000000",
        "status": {},
        "attributes": [
            {"key": "attention.outcome", "value": {"stringValue": outcome}},
            {"key": "body", "value": {"stringValue": "private text"}},
            {"key": "phone", "value": {"stringValue": "private peer"}},
        ],
        "links": list(links),
    }


def test_blocked_flow_keeps_missing_stages_not_reached_and_status_unset():
    canonical = reader.decode_trace(
        trace([span("attention.message", "a" * 16), span("autonomy.evaluate", "b" * 16)])
    )
    view = reader.project(canonical)
    assert view["metadata"][0]["outcome"] == "BLOCKED"
    rows = {r["span"]: r for r in view["stages"]}
    assert rows["autonomy.evaluate"]["reached"] == "REACHED"
    assert rows["autonomy.evaluate"]["status"] == "UNSET"
    assert rows["transport.outbound_send"]["reached"] == "NOT_REACHED"
    assert rows["transport.outbound_send"]["duration_ms"] is None
    assert "private" not in str(view)


def test_link_never_reparents_or_inflates_canonical_count():
    root = span(
        "attention.message",
        "a" * 16,
        outcome="ACCEPTED",
        links=[{"traceId": "2" * 32, "spanId": "b" * 16}],
    )
    inbound = span("transport.ingress_attempt", "b" * 16, parent="c" * 16)
    inbound["traceId"] = "2" * 32
    view = reader.project(reader.decode_trace(trace([root])), reader.decode_trace(trace([inbound])))
    assert view["metadata"][0]["span_count"] == 1
    assert view["metadata"][0]["relationship"] == "SPAN_LINK"
    assert view["metadata"][0]["link_target_status"] == "VERIFIED"
    rows = {r["name"]: r for r in view["latency"]}
    assert rows["attention.message"]["parent_span_id"] == ""
    assert rows["transport.ingress_attempt"]["parent_span_id"] == "c" * 16


def test_explicit_error_is_error_but_free_outcome_and_correlation_are_dropped():
    raw = span("attention.message", "a" * 16, outcome="private exception")
    raw["status"] = {"code": "STATUS_CODE_ERROR", "message": "private secret"}
    raw["attributes"].append(
        {"key": "roc.correlation_id", "value": {"stringValue": "private identifier"}}
    )
    view = reader.project(reader.decode_trace(trace([raw])))
    assert view["metadata"][0]["outcome"] == "ERROR"
    assert view["metadata"][0]["correlation_id"] == ""
    assert "private" not in str(view)


def test_immediate_path_is_not_mislabeled_as_missing_grace_error():
    class FakeTempo(reader.Tempo):
        def recent(self, start, end):
            return [{"traceID": "1" * 32}]

        def trace(self, trace_id):
            return reader.decode_trace(
                trace(
                    [
                        span("attention.message", "a" * 16),
                        span("queue.enqueue", "b" * 16, parent="a" * 16),
                    ]
                )
            )

    gaps = FakeTempo("http://unused").recent_view(0, 1)["gaps"][0]
    assert gaps["grace_path"] == "IMMEDIATE"
    assert gaps["grace_release"] == "NOT_REACHED"
    assert gaps["queue_enqueue"] == "REACHED"


def test_unknown_span_name_keeps_count_without_exporting_arbitrary_name():
    raw = trace([span("attention.message", "a" * 16), span("private-name", "b" * 16)])
    view = reader.project(reader.decode_trace(raw))
    assert view["metadata"][0]["span_count"] == 2
    assert "private" not in str(view)


def test_renderer_preserves_legacy_panels_when_run_again():
    import json

    spec = importlib.util.spec_from_file_location(
        "flow_dashboard", ROOT / "ops/observability/grafana/flow/build_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = json.loads(
        (ROOT / "ops/observability/grafana/dashboards/roc-e2e-operational.json").read_text()
    )
    regenerated = module.build(current)
    assert regenerated["panels"][-1]["panels"] == current["panels"][-1]["panels"]
    assert len(regenerated["panels"][-1]["panels"]) == 10
    assert regenerated["uid"] == current["uid"]
