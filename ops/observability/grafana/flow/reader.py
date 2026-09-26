"""Read-only, bounded Tempo projection for native Grafana panels; no Andy access."""

import base64
from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

STAGES = [
    ("Transport RX", "transport.receive"),
    ("Ingress attempt", "transport.ingress_attempt"),
    ("Ingress", "ingress.accept"),
    ("Message", "attention.message"),
    ("Grace", "grace.release"),
    ("Queue", "queue.enqueue"),
    ("Worker", "worker.dispatch"),
    ("Decision", "decision.evaluate"),
    ("Autonomy", "autonomy.evaluate"),
    ("Execution Intent", "execution.intent"),
    ("Outbox", "outbox.enqueue"),
    ("Transport TX", "transport.send"),
    ("Node Send", "transport.outbound_send"),
]
ORDER = [n for _, n in STAGES]
ORDER[4:4] = ["inbound.receive"]
ORDER[8:8] = [
    "event.normalize",
    "actor.resolve",
    "policy.resolve",
    "lab.session.claim_inbound",
    "andy.agent.context_build",
    "andy.agent.run",
]
ORDER.insert(ORDER.index("autonomy.evaluate"), "behavior.generate")
ORDER.insert(ORDER.index("execution.intent"), "repetition_guard.evaluate")
SERVICES = {"attention-router-transport", "attention-router-ingress", "attention-router-worker"}
OUTCOMES = {
    "ACCEPTED",
    "DELIVERED",
    "FAILED",
    "BLOCKED",
    "SUPPRESSED",
    "NO_RESPONSE",
    "PROCESSED",
    "ALLOWED",
    "DENIED",
    "CREATED",
    "QUEUED",
    "SENT",
    "DONE",
    "REQUIRES_APPROVAL",
    "OBSERVE_ONLY",
    "DRY_RUN",
    "RESPOND",
    "EVALUATED",
    "GENERATED",
    "NOT_APPLICABLE",
    "AUTO_ALLOWED",
    "ERROR",
    "OK",
}
SEARCH = (
    '{ span:name = "attention.message" && span.roc.trace_source = "native" '
    "&& span.roc.synthetic = false } with (most_recent = true)"
)
ID = re.compile(r"^[0-9a-f]{32}$")
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def identifier(value, size=32):
    if re.fullmatch("[0-9a-f]{" + str(size) + "}", value or ""):
        return value
    try:
        value = base64.b64decode(value, validate=True).hex()
        return value if len(value) == size else ""
    except (ValueError, TypeError):
        return ""


def attributes(items):
    return {x["key"]: next(iter(x.get("value", {}).values()), None) for x in items}


def decode_trace(data):
    spans = []
    for batch in data.get("batches", data.get("resourceSpans", [])):
        service = attributes(batch.get("resource", {}).get("attributes", [])).get("service.name")
        if service not in SERVICES:
            continue
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for raw in scope.get("spans", []):
                name = raw.get("name") if raw.get("name") in ORDER else "other.span"
                attr = attributes(raw.get("attributes", []))
                start, end = int(raw["startTimeUnixNano"]), int(raw["endTimeUnixNano"])
                code = raw.get("status", {}).get("code")
                outcome = attr.get("roc.result") or attr.get("attention.outcome")
                spans.append(
                    dict(
                        name=name,
                        service=service,
                        trace_id=identifier(raw.get("traceId")),
                        span_id=identifier(raw.get("spanId"), 16),
                        parent_span_id=identifier(raw.get("parentSpanId"), 16),
                        start_ms=start / 1e6,
                        end_ms=end / 1e6,
                        duration_ms=max(0, end - start) / 1e6,
                        status="ERROR"
                        if code in (2, "STATUS_CODE_ERROR")
                        else "OK"
                        if code in (1, "STATUS_CODE_OK")
                        else "UNSET",
                        outcome=outcome if outcome in OUTCOMES else "",
                        correlation_id=attr.get("roc.correlation_id")
                        if UUID.fullmatch(str(attr.get("roc.correlation_id", "")))
                        else "",
                        links=[
                            dict(
                                trace_id=identifier(x.get("traceId")),
                                span_id=identifier(x.get("spanId"), 16),
                            )
                            for x in raw.get("links", [])
                        ],
                    )
                )
    return spans


def summarize(spans):
    root = next((s for s in spans if s["name"] == "attention.message"), None)
    if root is None:
        raise ValueError("not a canonical trace")
    ordered = sorted(
        spans,
        key=lambda s: (ORDER.index(s["name"]) if s["name"] in ORDER else len(ORDER), s["start_ms"]),
    )
    final = next((s for s in reversed(ordered) if s["outcome"]), root)
    outcome = (
        "ERROR" if any(s["status"] == "ERROR" for s in spans) else final["outcome"] or "OBSERVED"
    )
    links = root["links"]
    return dict(
        trace_id=root["trace_id"],
        correlation_id=root["correlation_id"],
        root_span=root["name"],
        start_ms=min(s["start_ms"] for s in spans),
        end_ms=max(s["end_ms"] for s in spans),
        duration_ms=max(s["end_ms"] for s in spans) - min(s["start_ms"] for s in spans),
        service_count=len({s["service"] for s in spans}),
        span_count=len(spans),
        outcome=outcome,
        last_stage=ordered[-1]["name"],
        inbound_trace_id=links[0]["trace_id"] if links else "",
        inbound_span_id=links[0]["span_id"] if links else "",
        relationship="SPAN_LINK" if links else "NO_LINK_OBSERVED",
    )


def project(canonical, inbound=()):
    meta = summarize(canonical)
    linked = [s for s in inbound if s["trace_id"] == meta["inbound_trace_id"]]
    target = next((s for s in linked if s["span_id"] == meta["inbound_span_id"]), None)
    meta["link_target_status"] = (
        "VERIFIED"
        if target and target["name"] == "transport.ingress_attempt"
        else "UNRESOLVED"
        if meta["inbound_trace_id"]
        else "NO_LINK_OBSERVED"
    )
    all_spans = list(canonical) + linked
    rows = []
    for index, (stage, name) in enumerate(STAGES):
        found = [s for s in all_spans if s["name"] == name]
        # UNSET means the span exists without an explicit OTel status, never success by inference.
        status = (
            "ERROR"
            if any(s["status"] == "ERROR" for s in found)
            else "OK"
            if found and all(s["status"] == "OK" for s in found)
            else "UNSET"
            if found
            else "NOT_REACHED"
        )
        rows.append(
            dict(
                order=index + 1,
                stage=stage,
                span=name,
                reached="ERROR" if status == "ERROR" else "REACHED" if found else "NOT_REACHED",
                status=status,
                service=", ".join(sorted({s["service"] for s in found})),
                duration_ms=sum(s["duration_ms"] for s in found) if found else None,
                outcome=next((s["outcome"] for s in reversed(found) if s["outcome"]), ""),
                count=len(found),
            )
        )
    latency = [
        {
            k: s[k]
            for k in [
                "name",
                "service",
                "duration_ms",
                "status",
                "outcome",
                "trace_id",
                "span_id",
                "parent_span_id",
            ]
        }
        for s in sorted(
            all_spans,
            key=lambda s: (
                ORDER.index(s["name"]) if s["name"] in ORDER else len(ORDER),
                s["start_ms"],
            ),
        )
    ]
    counts = Counter(s["service"] for s in canonical)
    return dict(
        metadata=[meta],
        metadata_rows=[
            {"label": label, "value": str(value)}
            for label, value in [
                ("Trace ID", meta["trace_id"]),
                ("Correlation ID", meta["correlation_id"]),
                (
                    "Início",
                    datetime.fromtimestamp(meta["start_ms"] / 1000, timezone.utc).isoformat(),
                ),
                ("Fim", datetime.fromtimestamp(meta["end_ms"] / 1000, timezone.utc).isoformat()),
                ("Duração total", f"{meta['duration_ms'] / 1000:.3f} s"),
                ("Serviços", meta["service_count"]),
                ("Spans canônicos", meta["span_count"]),
                ("Resultado observado", meta["outcome"]),
                ("Última etapa", meta["last_stage"]),
            ]
        ],
        links=[
            {
                "label": "Canonical",
                "trace_id": meta["trace_id"],
                "relationship": meta["relationship"],
                "link_target_status": meta["link_target_status"],
            },
            {
                "label": "Inbound",
                "trace_id": meta["inbound_trace_id"],
                "relationship": meta["relationship"],
                "link_target_status": meta["link_target_status"],
            },
        ]
        if meta["inbound_trace_id"]
        else [],
        stages=rows,
        latency=latency,
        services=[dict(service=k, span_count=v) for k, v in sorted(counts.items())],
    )


class Tempo:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.cache = {}
        self.lock = threading.RLock()

    def get(self, path, ttl=10):
        # Single flight across panels; bounded cache, responses and query concurrency.
        with self.lock:
            now = time.monotonic()
            entry = self.cache.get(path)
            if entry and entry[0] > now:
                return entry[1]
            with urlopen(self.url + path, timeout=12) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("response limit")
            result = json.loads(raw)
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            if len(self.cache) >= 256:
                self.cache.clear()
            self.cache[path] = (now + ttl, result)
            return result

    def trace(self, trace_id):
        if not ID.fullmatch(trace_id):
            raise ValueError("invalid trace id")
        return decode_trace(self.get("/api/traces/" + trace_id))

    def recent(self, start, end):
        query = urlencode(dict(q=SEARCH, start=start, end=end, limit=12, spss=1))
        result = self.get("/api/search?" + query)
        return sorted(
            result.get("traces", []), key=lambda x: int(x["startTimeUnixNano"]), reverse=True
        )

    def view(self, trace_id, start, end):
        if trace_id in ("", "latest"):
            recent = self.recent(start, end)
            if not recent:
                return dict(
                    metadata=[], metadata_rows=[], links=[], stages=[], latency=[], services=[]
                )
            trace_id = recent[0]["traceID"]
        canonical = self.trace(trace_id)
        meta = summarize(canonical)
        inbound = []
        if meta["inbound_trace_id"]:
            try:
                inbound = self.trace(meta["inbound_trace_id"])
            except HTTPError as exc:
                if exc.code != 404:
                    raise
        return project(canonical, inbound)

    def recent_view(self, start, end):
        traces, gaps = [], []
        for hit in self.recent(start, end):
            try:
                canonical = self.trace(hit["traceID"])
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            meta = summarize(canonical)
            traces.append(meta)
            row = {k: meta[k] for k in ["trace_id", "correlation_id", "start_ms", "outcome"]}
            for _, name in STAGES[3:]:
                found = [s for s in canonical if s["name"] == name]
                row[name.replace(".", "_")] = (
                    "ERROR"
                    if any(s["status"] == "ERROR" for s in found)
                    else "REACHED"
                    if found
                    else "NOT_REACHED"
                )
            row["grace_path"] = (
                "GRACE"
                if any(s["name"] == "grace.release" for s in canonical)
                else "IMMEDIATE"
                if any(
                    s["name"] == "queue.enqueue"
                    and s["parent_span_id"]
                    == next(s["span_id"] for s in canonical if s["name"] == "attention.message")
                    for s in canonical
                )
                else "NOT_REACHED"
            )
            gaps.append(row)
        return dict(traces=traces, gaps=gaps)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # No request URL, arbitrary text or identifiers in access logs.

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            args = parse_qs(parsed.query)
            now = int(time.time())
            end = min(int(args.get("end", [now * 1000])[0]) // 1000, now) // 10 * 10
            start = (
                max(int(args.get("start", [(end - 900) * 1000])[0]) // 1000, end - 86400) // 10 * 10
            )
            if end <= start:
                raise ValueError("invalid range")
            if parsed.path == "/health":
                data = {"status": "ok"}
            elif parsed.path == "/view":
                data = self.server.tempo.view(args.get("trace_id", ["latest"])[0], start, end)
            elif parsed.path == "/recent":
                data = self.server.tempo.recent_view(start, end)
            else:
                self.send_error(404)
                return
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(200)
        except ValueError:
            body = b'{"error":"INVALID_SELECTION"}'
            self.send_response(400)
        except Exception:
            body = b'{"error":"TEMPO_UNAVAILABLE"}'
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.tempo = Tempo(os.environ.get("TEMPO_URL", "http://roc-tempo:3200"))
    server.serve_forever()
