"""New dashboard only; never patch/restart an existing operational dashboard."""
from __future__ import annotations

import json
from pathlib import Path
import sys

PARENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PARENT))
from node_graph_panel import panel  # noqa: E402


def build():
    graph = panel()
    return {
        "id": None,
        "uid": "roc-live-message-path-v1",
        "title": "Andy · Live Message Path V1",
        "description": (
            "Mapa lógico de serviço; saúde via Zabbix com timestamp, percurso via "
            "Tempo, rede somente quando houver evidência independente. "
            "NOT_REACHED != falha; UNKNOWN != DOWN."
        ),
        "tags": ["andy", "roc", "flow-map", "evidence-first"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 0,
        "refresh": "30s",
        "time": {"from": "now-15m", "to": "now"},
        "templating": {"list": [{
            "name": "trace_id",
            "type": "textbox",
            "label": "Trace canônico",
            "query": "latest",
            "current": {"text": "latest", "value": "latest"},
            "hide": 0,
        }]},
        "panels": [graph],
        "annotations": {"list": []},
        "editable": False,
        "links": [{
            "title": "ROC-E2E · Operational Flow",
            "type": "link",
            "url": "/d/roc-e2e-operational/roc-e2e-operational-flow",
            "targetBlank": False,
        }],
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, ensure_ascii=False))
