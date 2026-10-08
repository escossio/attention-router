"""Staged Node Graph panel definition; not added to production dashboard.

Requires a future /map endpoint providing bounded, authorized projections of
fresh ROC Zabbix history and selected Tempo traces. No fallback to fake health.
"""
from __future__ import annotations

FLOW = {"type": "yesoreyeram-infinity-datasource", "uid": "roc-flow"}
URL = (
    "http://roc-flow-reader:8080/map"
    "?trace_id=${trace_id:percentencode}&start=${__from}&end=${__to}"
)
NODE_FIELDS = (
    "id", "title", "subTitle", "mainStat", "secondaryStat", "color",
    "detail__health_source", "detail__evidence", "detail__host",
)
EDGE_FIELDS = (
    "id", "source", "target", "mainStat", "secondaryStat",
    "detail__relationship", "detail__sensor_quality",
)


def query(ref_id: str, root: str, fmt: str, fields: tuple[str, ...]) -> dict:
    if not (ref_id in {"A", "B"}
            and (root, fmt) in (("nodes", "node-graph-nodes"),
                                ("edges", "node-graph-edges"))):
        raise ValueError("unsupported Node Graph query")
    return {
        "refId": ref_id,
        "datasource": FLOW.copy(),
        "type": "json",
        "source": "url",
        "format": fmt,
        "parser": "backend",
        "url": URL,
        "root_selector": root,
        "columns": [
            {"selector": field, "text": field, "type": "string"}
            for field in fields
        ],
        "url_options": {"method": "GET"},
        "filters": [],
    }


def panel(pid: int = 110) -> dict:
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid panel id")
    return {
        "id": pid,
        "type": "nodeGraph",
        "title": "Andy Live Message Path · evidence-first",
        "description": (
            "Nodes = observed Zabbix Docker entities, health with freshness; "
            "edges = declared topology or validated OTel relationship. "
            "TCP witness never implies correlation with selected trace. "
            "This panel must NOT be provisioned before /map is live and verified."
        ),
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 18},
        "datasource": FLOW.copy(),
        "targets": [
            query("A", "nodes", "node-graph-nodes", NODE_FIELDS),
            query("B", "edges", "node-graph-edges", EDGE_FIELDS),
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "nodes": {},
            "edges": {},
        },
    }
