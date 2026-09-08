import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "ops/observability/grafana/dashboards/attention-router-message-journey.json"
DATASOURCE = ROOT / "ops/observability/grafana/provisioning/datasources/tempo.yaml"


def _dashboard():
    return json.loads(DASHBOARD.read_text())


def _panels():
    return {panel["id"]: panel for panel in _dashboard()["panels"]}


def test_message_journey_targets_use_current_tempo_and_safe_queries():
    panels = _panels()
    targets = [panel["targets"][0] for panel in panels.values() if panel.get("datasource")]

    assert targets
    assert {
        panel_id: panel["datasource"]
        for panel_id, panel in panels.items() if panel.get("datasource")
    } == {
        2: {"type": "postgres", "uid": "attention-router-postgres"},
        3: {"type": "postgres", "uid": "attention-router-postgres"},
        4: {"type": "postgres", "uid": "attention-router-postgres"},
        5: {"type": "postgres", "uid": "attention-router-postgres"},
        6: {"type": "tempo", "uid": "attention-router-tempo"},
    }

    selected = panels[6]
    assert selected["type"] == "traces"
    assert selected["targets"][0]["queryType"] == "traceql"
    assert selected["targets"][0]["query"] == '{ trace:id = "$trace_id" }'
    assert "761e63c142dcea437f02bb9b37adb931" not in selected["targets"][0]["query"]

    recent = panels[2]
    assert recent["type"] == "table"
    assert recent["targets"][0]["rawQuery"] is True
    assert recent["targets"][0]["format"] == "table"
    assert recent["targets"][0]["datasource"] == recent["datasource"]
    query = recent["targets"][0]["rawSql"]
    assert "FROM inbound_events e JOIN interactions i ON i.id=e.interaction_id" in query
    assert "${contact_filter}" in query
    assert "[REDACTED]" in query
    assert query.endswith("FROM x WHERE rn=1 ORDER BY received_at DESC LIMIT 20")
    for panel_id in (3, 4, 5):
        panel = panels[panel_id]
        assert panel["type"] == "table"
        assert panel["targets"][0]["rawQuery"] is True
        assert panel["targets"][0]["format"] == "table"
        assert "${interaction_id}" in panel["targets"][0]["rawSql"]
    assert selected["targets"][0]["limit"] == 1


def test_message_journey_uses_dashboard_range_and_non_streaming_search():
    dashboard = _dashboard()
    assert dashboard["time"] == {"from": "now-24h", "to": "now"}
    datasource = DATASOURCE.read_text()
    assert "uid: attention-router-tempo" in datasource
    assert "streamingEnabled:\n        search: false" in datasource
