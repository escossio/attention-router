"""Offline contract checks; the dashboard remains unchanged."""
import importlib.util
from pathlib import Path

BASE = Path(__file__).parents[1]
FILE = BASE / "ops/observability/grafana/flow/node_graph_panel.py"
SPEC = importlib.util.spec_from_file_location("node_graph_panel", FILE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_staged_nodegraph_queries_use_installed_infinity4_format():
    p = module.panel()
    assert p["type"] == "nodeGraph"
    assert p["id"] == 110
    assert p["datasource"]["uid"] == "roc-flow"
    assert [q["format"] for q in p["targets"]] == [
        "node-graph-nodes", "node-graph-edges",
    ]
    assert [q["root_selector"] for q in p["targets"]] == ["nodes", "edges"]
    assert [q["refId"] for q in p["targets"]] == ["A", "B"]
    assert all(q["parser"] == "backend" for q in p["targets"])
    assert all("trace_id=${trace_id:percentencode}" in q["url"] for q in p["targets"])


def test_nodegraph_requires_actual_source_and_no_fake_fallback():
    p = module.panel()
    assert all("/map?" in q["url"] for q in p["targets"])
    assert all(q["source"] == "url" for q in p["targets"])
    assert not any(q.get("data") for q in p["targets"])
    node_columns = {x["selector"] for x in p["targets"][0]["columns"]}
    edge_columns = {x["selector"] for x in p["targets"][1]["columns"]}
    assert {"id", "title", "mainStat", "secondaryStat"}.issubset(node_columns)
    assert {"id", "source", "target"}.issubset(edge_columns)
    assert "detail__health_source" in node_columns
    assert "detail__relationship" in edge_columns


def test_dashboard_remains_unmodified_and_staged_panel_has_unique_id():
    import json
    data = json.loads((BASE / "ops/observability/grafana/dashboards/roc-e2e-operational.json").read_text())
    def flatten(panels):
        for x in panels:
            yield x["id"]
            if "panels" in x:
                yield from flatten(x["panels"])
    assert module.panel()["id"] not in set(flatten(data["panels"]))
