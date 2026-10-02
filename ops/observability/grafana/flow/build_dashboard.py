"""Preserve the existing ROC dashboard, adding only native operational panels."""

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import quote

FLOW = {"type": "yesoreyeram-infinity-datasource", "uid": "roc-flow"}
TEMPO = {"type": "tempo", "uid": "roc-tempo"}
DASH = "/d/roc-e2e-operational/roc-e2e-operational-flow"
BASE_URL = "http://roc-flow-reader:8080"
VIEW = BASE_URL + "/view?trace_id=${trace_id:percentencode}&start=${__from}&end=${__to}"
RECENT = BASE_URL + "/recent?start=${__from}&end=${__to}"
COLORS = {
    "OK": "green",
    "REACHED": "green",
    "DELIVERED": "green",
    "VERIFIED": "green",
    "ERROR": "red",
    "FAILED": "red",
    "BLOCKED": "orange",
    "SUPPRESSED": "orange",
    "NOT_REACHED": "gray",
    "UNSET": "blue",
    "NO_RESPONSE": "blue",
}


def explore(value):
    panes = {
        "a": {
            "datasource": "roc-tempo",
            "queries": [
                {"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": "__ID__"}
            ],
            "range": {"from": "now-24h", "to": "now"},
        }
    }
    return "/explore?schemaVersion=1&orgId=1&panes=" + quote(
        json.dumps(panes, separators=(",", ":"))
    ).replace("__ID__", value)


def query(root, columns, url=VIEW):
    return dict(
        refId="A",
        datasource=FLOW,
        type="json",
        source="url",
        format="table",
        parser="backend",
        url=url,
        root_selector=root,
        columns=[dict(selector=k, text=k, type=t) for k, t in columns],
        url_options={"method": "GET"},
        filters=[],
    )


def override(name, properties):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": k, "value": v} for k, v in properties.items()],
    }


def table(pid, title, root, columns, x, y, w, h, description="", url=VIEW):
    labels = {
        "trace_id": "Trace ID",
        "correlation_id": "Correlation ID",
        "start_ms": "Início",
        "end_ms": "Fim",
        "duration_ms": "Duração",
        "service_count": "Serviços",
        "span_count": "Spans",
        "outcome": "Resultado observado",
        "root_span": "Root span",
        "last_stage": "Última etapa observada",
        "stage": "Etapa",
        "span": "Span",
        "service": "Service",
        "status": "OTel status",
        "reached": "Presença",
        "name": "Span",
        "inbound_trace_id": "Inbound trace",
        "relationship": "Relação",
        "link_target_status": "Link",
        "parent_span_id": "Parent span ID",
        "label": "Campo",
        "value": "Valor",
        "span_id": "Span ID",
        "count": "Chamadas / spans",
        "grace_path": "Caminho",
    }
    overrides = []
    for name, typ in columns:
        props = {"displayName": labels.get(name, name)}
        if name in ("start_ms", "end_ms"):
            props["unit"] = "dateTimeAsIso"
            props["custom.width"] = 185
        if name == "duration_ms":
            props.update(unit="ms", decimals=2)
        if name in ("trace_id", "inbound_trace_id"):
            props["links"] = [
                {
                    "title": "ABRIR TRACE NO TEMPO / EXPLORE",
                    "url": explore("${__value.raw}"),
                    "targetBlank": True,
                }
            ]
            if name == "trace_id":
                props["links"].insert(
                    0,
                    {
                        "title": "Selecionar trace neste dashboard",
                        "url": DASH + "?var-trace_id=${__value.raw}&from=${__from}&to=${__to}",
                    },
                )
            props["custom.width"] = 260
        if name == "correlation_id":
            props["custom.width"] = 285
        if name in ("reached", "status", "outcome", "link_target_status") or name.startswith(
            (
                "attention_",
                "attention.",
                "grace_",
                "grace.",
                "queue_",
                "queue.",
                "worker_",
                "worker.",
                "decision_",
                "decision.",
                "autonomy_",
                "autonomy.",
                "execution_",
                "execution.",
                "outbox_",
                "outbox.",
                "transport_",
                "transport.",
            )
        ):
            props["custom.cellOptions"] = {"type": "color-text"}
            props["mappings"] = [
                {
                    "type": "value",
                    "options": {k: {"text": k, "color": v} for k, v in COLORS.items()},
                }
            ]
        overrides.append(override(name, props))
    return dict(
        id=pid,
        type="table",
        title=title,
        description=description,
        gridPos=dict(x=x, y=y, w=w, h=h),
        datasource=FLOW,
        targets=[query(root, columns, url)],
        transformations=[
            {
                "id": "organize",
                "options": {"indexByName": {k: i for i, (k, _) in enumerate(columns)}},
            }
        ],
        fieldConfig={
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True},
                "noValue": "—",
            },
            "overrides": overrides,
        },
        options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    )


def trace_panel(pid, title, var, y, h):
    return dict(
        id=pid,
        type="traces",
        title=title,
        gridPos=dict(x=0, y=y, w=24, h=h),
        datasource=TEMPO,
        description="Árvore nativa do Tempo. Expanda spans para ver duração, serviço, status e parent. Um Span Link não é parent/child.",
        targets=[
            {"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": "${" + var + "}"}
        ],
        options={"spanBar": {"type": "Duration"}, "spanFilters": {"filters": []}},
        fieldConfig={"defaults": {}, "overrides": []},
    )


def variable(name, field):
    return dict(
        name=name,
        type="query",
        hide=2,
        datasource=FLOW,
        refresh=2,
        sort=0,
        multi=False,
        includeAll=False,
        query={
            "queryType": "infinity",
            "refId": "variable",
            "infinityQuery": query("metadata", [(field, "string")]),
            "meta": {"textField": field, "valueField": field},
        },
        current={"text": "", "value": ""},
        options=[],
        skipUrlSync=True,
    )


def build(original):
    dashboard = copy.deepcopy(original.get("dashboard", original))
    assert dashboard["uid"] == "roc-e2e-operational"
    legacy_row = next(
        (p for p in dashboard["panels"] if p["id"] == 109 and p["type"] == "row"), None
    )
    original_panels = (
        legacy_row["panels"] if legacy_row else [p for p in dashboard["panels"] if p["id"] < 100]
    )
    if legacy_row:
        for panel in original_panels:
            panel["gridPos"]["y"] -= legacy_row["gridPos"]["y"] + 1
    # Original positions are retained within a collapsed native infrastructure row.
    panels = []
    panels.append(
        table(
            100,
            "Andy E2E Flow",
            "stages",
            [
                (k, t)
                for k, t in [
                    ("order", "number"),
                    ("stage", "string"),
                    ("span", "string"),
                    ("reached", "string"),
                    ("status", "string"),
                    ("duration_ms", "number"),
                    ("service", "string"),
                    ("outcome", "string"),
                ]
            ],
            0,
            0,
            24,
            13,
            "Ordem causal; INBOUND é um trace separado ligado por SPAN_LINK. NOT_REACHED = ausência no trace consultado, nunca falha inferida. UNSET = span sem status explícito. trace_id=latest acompanha o último canônico no intervalo.",
        )
    )
    panels.append(
        table(
            101,
            "Último trace canônico / trace selecionado",
            "metadata",
            [
                (k, t)
                for k, t in [
                    ("trace_id", "string"),
                    ("correlation_id", "string"),
                    ("start_ms", "number"),
                    ("end_ms", "number"),
                    ("duration_ms", "number"),
                    ("service_count", "number"),
                    ("span_count", "number"),
                    ("outcome", "string"),
                    ("last_stage", "string"),
                ]
            ],
            0,
            13,
            24,
            5,
            "Duração total = último término menos primeiro início, incluindo espera assíncrona. Resultado observado vem da última etapa com outcome; a ausência de envio não implica falha.",
        )
    )
    panels.append(
        table(
            102,
            "Inbound ↔ Canonical · SPAN_LINK",
            "metadata",
            [
                (k, "string")
                for k in [
                    "trace_id",
                    "inbound_trace_id",
                    "relationship",
                    "inbound_span_id",
                    "link_target_status",
                ]
            ],
            0,
            18,
            24,
            4,
            "attention.message referencia transport.ingress_attempt por Span Link. Os traces conservam árvores parent/child separadas. Ambos os IDs abrem o Tempo.",
        )
    )
    panels.append(trace_panel(103, "Trace completo · canonical", "resolved_trace_id", 22, 18))
    panels.append(
        table(
            104,
            "Latência por estágio · ordem causal",
            "latency",
            [
                (k, t)
                for k, t in [
                    ("name", "string"),
                    ("service", "string"),
                    ("duration_ms", "number"),
                    ("status", "string"),
                    ("outcome", "string"),
                ]
            ],
            0,
            40,
            17,
            15,
            "Inclui spans canônicos e inbound vinculado. Durações de spans aninhados se sobrepõem; não devem ser somadas como duração total. A espera de Grace é visível no intervalo entre spans da árvore.",
        )
    )
    panels.append(
        table(
            105,
            "Serviços observados no trace canônico",
            "services",
            [("service", "string"), ("span_count", "number")],
            17,
            40,
            7,
            7,
            "Processos reais. Service Graph agregado indisponível: este Tempo não tem metrics-generator/Prometheus configurado. Grace, Queue e Decision continuam spans, nunca serviços fictícios.",
        )
    )
    panels.append(
        table(
            106,
            "Recent E2E Traces · native",
            "traces",
            [
                (k, t)
                for k, t in [
                    ("start_ms", "number"),
                    ("trace_id", "string"),
                    ("correlation_id", "string"),
                    ("root_span", "string"),
                    ("duration_ms", "number"),
                    ("span_count", "number"),
                    ("outcome", "string"),
                    ("inbound_trace_id", "string"),
                ]
            ],
            0,
            55,
            24,
            10,
            "Até 12 traces nativos mais recentes; busca limitada a 24 h e cache de 10 s. Clique no Trace ID para selecionar ou abrir no Tempo. Nenhum conteúdo de mensagem é consultado na Andy.",
            RECENT,
        )
    )
    gaps = [
        "attention.message",
        "grace.release",
        "queue.enqueue",
        "worker.dispatch",
        "decision.evaluate",
        "autonomy.evaluate",
        "execution.intent",
        "outbox.enqueue",
        "transport.send",
        "transport.outbound_send",
    ]
    panels.append(
        table(
            107,
            "Flow gaps · REACHED / NOT_REACHED / ERROR",
            "gaps",
            [
                ("start_ms", "number"),
                ("trace_id", "string"),
                ("outcome", "string"),
                ("grace_path", "string"),
            ]
            + [(k.replace(".", "_"), "string") for k in gaps],
            0,
            65,
            24,
            10,
            "Ausências não são erros. IMMEDIATE indica queue.enqueue filho direto de attention.message, sem Grace. Bloqueio, aprovação e execução desabilitada podem terminar legitimamente antes do Outbox.",
            RECENT,
        )
    )
    panels.append(
        trace_panel(108, "Trace completo · inbound (árvore separada)", "inbound_trace_id", 75, 10)
    )
    for panel in original_panels:
        panel["gridPos"]["y"] += 92
    panels.append(
        dict(
            id=109,
            type="row",
            title="Infraestrutura Zabbix e traces reconstruídos · preservados",
            collapsed=True,
            gridPos=dict(x=0, y=91, w=24, h=1),
            panels=original_panels,
        )
    )
    panels[0]["gridPos"]["h"] = 16
    panels[0]["transformations"][0]["options"] = {
        "indexByName": {
            k: i
            for i, k in enumerate(
                ["stage", "reached", "status", "duration_ms", "service", "span", "outcome", "order"]
            )
        },
        "excludeByName": {"order": True},
    }
    for key, width in [
        ("stage", 145),
        ("reached", 135),
        ("status", 95),
        ("duration_ms", 100),
        ("service", 280),
        ("span", 260),
        ("outcome", 145),
    ]:
        panels[0]["fieldConfig"]["overrides"].append(override(key, {"custom.width": width}))
    panels[1] = table(
        101,
        "Último trace canônico / trace selecionado",
        "metadata_rows",
        [("label", "string"), ("value", "string")],
        0,
        16,
        14,
        12,
        "Início/fim em UTC; duração total inclui espera assíncrona. IDs completos para copiar.",
    )
    panels[1]["fieldConfig"]["overrides"].append(override("label", {"custom.width": 155}))
    panels[2] = table(
        102,
        "Inbound ↔ Canonical · SPAN_LINK",
        "links",
        [
            ("label", "string"),
            ("trace_id", "string"),
            ("relationship", "string"),
            ("link_target_status", "string"),
        ],
        14,
        16,
        10,
        6,
        "Árvores separadas. Clique em cada Trace ID para abrir o Tempo. VERIFIED confirma o span alvo transport.ingress_attempt.",
    )
    panels[2]["fieldConfig"]["overrides"].append(override("label", {"custom.width": 95}))
    for field in panels[2]["fieldConfig"]["overrides"]:
        for prop in field["properties"]:
            if prop["id"] == "links":
                prop["value"] = [
                    link
                    for link in prop["value"]
                    if link["title"] == "ABRIR TRACE NO TEMPO / EXPLORE"
                ]
    for panel in panels[3:]:
        if panel["id"] != 109:
            panel["gridPos"]["y"] += 6
    dashboard.update(
        panels=panels,
        refresh="10s",
        time={"from": "now-1h", "to": "now"},
        version=dashboard.get("version", 0) + 1,
    )
    dashboard.pop("id", None)
    dashboard["tags"] = sorted(set(dashboard.get("tags", []) + ["native-otel", "e2e-flow"]))
    dashboard["templating"] = {
        "list": [
            dict(
                name="trace_id",
                label="Trace ID · latest = automático",
                type="textbox",
                current={"text": "latest", "value": "latest"},
                query="latest",
                options=[{"text": "latest", "value": "latest", "selected": True}],
                hide=0,
            ),
            variable("resolved_trace_id", "trace_id"),
            variable("inbound_trace_id", "inbound_trace_id"),
        ]
    }
    dashboard["links"] = [
        {
            "type": "link",
            "title": "Último trace (automático)",
            "url": DASH + "?var-trace_id=latest&refresh=10s",
        },
        {
            "type": "link",
            "title": "Canário E2E certificado",
            "url": DASH + "?var-trace_id=1bb1ad92b493e7dc58d429ce8d260172&refresh=10s",
        },
        {"type": "link", "title": "TraceQL / Tempo", "url": explore("${resolved_trace_id}")},
        {
            "type": "link",
            "title": "Ajuda: traces nativos",
            "url": "https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/traces/",
            "targetBlank": True,
        },
    ]
    return dashboard


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("existing")
    parser.add_argument("output")
    args = parser.parse_args()
    Path(args.output).write_text(
        json.dumps(build(json.loads(Path(args.existing).read_text())), ensure_ascii=False, indent=2)
        + "\n"
    )
