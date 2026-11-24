"""Generate the versioned ROC-E2E G7 Grafana dashboard."""

import json
from pathlib import Path

BASE = "http://127.0.0.1:19491"
DATASOURCE = {"type": "yesoreyeram-infinity-datasource", "uid": "roc-g7"}
VIEW = (BASE + "/view?run=${run:percentencode}&baseline=${baseline:percentencode}"
        "&trace=${trace:percentencode}")
NUMBER = "number"
STRING = "string"


def target(selector, columns, *, url=VIEW, ref="A"):
    return {"refId": ref, "datasource": DATASOURCE, "type": "json", "source": "url",
            "format": "table", "parser": "backend", "url": url, "root_selector": selector,
            "columns": [{"selector": name, "text": name, "type": kind} for name, kind in columns],
            "url_options": {"method": "GET"}, "filters": []}


def variable(name, label, selector):
    return {"name": name, "label": label, "type": "query", "datasource": DATASOURCE, "refresh": 2,
            "query": {"queryType": "infinity", "refId": "variable", "infinityQuery": target(
                selector, [("id", STRING), ("label", STRING)], url=BASE + "/runs"),
                "meta": {"textField": "label", "valueField": "id"}},
            "current": {"text": "M1 histórico", "value": "historical"} if name == "baseline" else {}}


def build():
    panels = []
    y = 0

    def row(title):
        nonlocal y
        panels.append({"id": len(panels) + 1, "type": "row", "title": title, "collapsed": False,
                       "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        y += 1

    def panel(title, selector, strings=(), numbers=(), *, height=6, kind="table", fields=None):
        nonlocal y
        x = {"id": len(panels) + 1, "type": kind, "title": title, "datasource": DATASOURCE,
             "gridPos": {"x": 0, "y": y, "w": 24, "h": height},
             "targets": [target(selector, [(name, STRING) for name in strings] +
                                [(name, NUMBER) for name in numbers])],
             "options": {"showHeader": True}}
        if kind == "stat":
            x["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
                            "textMode": "value_and_name", "orientation": "horizontal", "colorMode": "none"}
        if fields:
            x["fieldConfig"] = fields
        panels.append(x)
        y += height

    row("1 · Saúde do ensaio M1")
    panel("Oferta, admissão e concorrência", "summary", numbers=(
        "offered", "admitted", "in_progress", "executor_waiting", "worker_active", "completed",
        "throughput_per_min"), kind="stat", height=4)
    panel("Disponibilidade, fundamentação e latência acumulada", "summary", numbers=(
        "availability_pct", "grounding_pct", "grounded", "abstained", "p50_ms", "p95_ms", "p99_ms",
        "client_timeout", "server_empty", "disconnects", "cancelled"), kind="stat", height=4)
    panel("Comparação por execução", "comparison", strings=("metric",),
          numbers=("selected", "baseline", "delta", "delta_pct"))
    panel("Comparabilidade de ambiente e configuração", "comparability", strings=("comparable", "differences"),
          height=3)
    panel("Cadência de ofertas, admissões e conclusões", "evolution",
          numbers=("time", "offered", "admitted", "completed"), kind="timeseries")
    panels[-1]["targets"][0]["columns"][0]["type"] = "timestamp"

    row("2 · Localização")
    panel("Componentes por p95. Tempos inclusivos, espera e driver SQL", "components",
          strings=("stage", "kind"), numbers=("samples", "p50_ms", "p95_ms", "p99_ms", "driver_ms", "sql_count"),
          height=9)
    panel("Mudança de p95 por componente", "component_comparison", strings=("stage", "kind"),
          numbers=("selected_ms", "baseline_ms", "delta_ms"))
    panel("Distribuição acumulada de latência", "distribution", numbers=("upper_ms", "cumulative"))
    panels[-1]["targets"].append(target("baseline_distribution",
                                       [("upper_ms", NUMBER), ("cumulative", NUMBER)], ref="B"))
    panel("Desfechos do servidor, incl. contenção e autoridade", "outcomes", strings=("outcome",),
          numbers=("count",))
    panel("Amostras de ocupação. Pool PostgreSQL e executor AnyIO", "capacity",
          strings=("resource",), numbers=("time", "used", "limit", "waiting"), height=6)
    panel("CPU, pico de RSS e I/O do processo de bancada", "resources",
          numbers=("time", "cpu_seconds", "rss_peak_kib", "input_blocks", "output_blocks"), height=6)
    panels[-1]["targets"].append(target("baseline_resources", [
        ("time", NUMBER), ("cpu_seconds", NUMBER), ("rss_peak_kib", NUMBER),
        ("input_blocks", NUMBER), ("output_blocks", NUMBER),
    ], ref="B"))

    row("3 · Investigação por consulta")
    panel("Caminho observado e espera sem sobreposição", "traces", strings=("trace", "result"),
          numbers=("server_ms", "observed_wait_ms", "unknown_or_processing_ms"), height=9,
          fields={"defaults": {}, "overrides": [{"matcher": {"id": "byName", "options": "trace"},
                  "properties": [{"id": "links", "value": [{
                      "title": "Ver eventos correlacionados", "url": (
                          "/d/roc-g7-contextual-m1?var-run=${run:percentencode}"
                          "&var-baseline=${baseline:percentencode}&var-trace=${__value.raw}"
                      )}, {"title": "Abrir trace no Tempo", "url": (
                          "/explore?schemaVersion=1&panes=%7B%22g7%22%3A%7B%22datasource%22"
                          "%3A%22roc-tempo%22%2C%22queries%22%3A%5B%7B%22queryType%22%3A%22traceql%22"
                          "%2C%22query%22%3A%22${__value.raw}%22%2C%22refId%22%3A%22A%22%7D%5D%7D%7D"
                      )}]}]}]})
    panel("Eventos correlacionados e SQL agregado da consulta", "timeline",
          strings=("event", "stage", "outcome", "correlation"),
          numbers=("timestamp_ns", "duration_ns", "sql_count"), height=8)
    panel("Identidade e configuração efetiva do ensaio", "metadata", strings=("field", "value"), height=8)
    panel("Último evento visto pela projeção", "summary",
          numbers=("latest_event_ns", "latest_event_age_ms", "read_ns"), height=4)
    return {"uid": "roc-g7-contextual-m1", "title": "G7 / Bancada M1 de contexto", "schemaVersion": 41,
            "version": 1, "refresh": "5s", "tags": ["ROC-E2E", "G7", "experiment"],
            "time": {"from": "now-30m", "to": "now"}, "panels": panels, "templating": {"list": [
                variable("run", "Execução", "runs"), variable("baseline", "Comparar com", "baselines"),
                {"name": "trace", "label": "Trace para investigação", "type": "textbox", "query": "",
                 "current": {"text": "", "value": ""}},
            ]}}


if __name__ == "__main__":
    target_path = Path(__file__).with_name("roc-g7-contextual-m1.json")
    target_path.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
