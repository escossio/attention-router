"""Least-privilege Flow Map HTTP bridge: Zabbix view + Tempo, never Andy/DB writes."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

try:
    import psycopg
except ImportError:
    psycopg = None

import flow_map
import reader

SERVICE_ENTITIES = {
    "attention-router-transport": "andy-whatsapp-runtime/transport/1",
    "attention-router-ingress": "attention-router/ingress/1",
    "attention-router-worker": "attention-router-live-flow-stage4/worker/1",
}
EXPECTED_EDGES = [
    ("attention-router-transport", "attention-router-ingress"),
    ("attention-router-ingress", "attention-router-worker"),
]
MAX_ROWS = 32
MAX_REQUEST_SECONDS = 86400
MAX_AGE_SECONDS = 180
DB_ROLE = "roc_flow_map_v1_reader"
DB_VIEW = "public.roc_flow_map_v1_inventory"
VALID_ID = re.compile(r"^[0-9a-fA-F]{31,32}$")
QUERY = "SELECT kind,clock,value FROM public.roc_flow_map_v1_inventory"


class InvalidSelection(ValueError):
    """Malformed or unbounded user-facing Grafana selection."""


class ZabbixView:
    def __init__(self, connector=None):
        self.connector = connector or (psycopg.connect if psycopg else None)

    def read(self):
        if self.connector is None:
            raise RuntimeError("psycopg unavailable")
        # PostgreSQL role has SELECT on the view ONLY; connection is also read-only.
        with self.connector(
            host=os.environ.get("ROC_ZABBIX_HOST", "roc-zabbix-db"),
            dbname="zabbix",
            user=DB_ROLE,
            passfile=os.environ.get("PGPASSFILE", "/run/secrets/zabbix_pgpass"),
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=3500",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(QUERY)
                rows = cursor.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            raise ValueError("inventory size limit")
        containers, attachments = [], []
        for kind, clock, value in rows:
            if kind not in ("container", "attachment"):
                raise ValueError("unexpected inventory object")
            if not isinstance(value, dict) or value.get("entity_key") not in SERVICE_ENTITIES.values():
                raise ValueError("inventory scope violation")
            sample = {"clock": int(clock), "value": value}
            (containers if kind == "container" else attachments).append(sample)
        return containers, attachments


class MapProjection:
    def __init__(self, zabbix=None, tempo=None):
        self.zabbix = zabbix or ZabbixView()
        self.tempo = tempo or reader.Tempo(os.environ.get("TEMPO_URL", "http://roc-tempo:3200"))
        self.lock = threading.Lock()

    def view(self, trace_id, start_ms, end_ms):
        if trace_id not in ("", "latest") and not VALID_ID.fullmatch(trace_id):
            raise InvalidSelection("invalid trace id")
        now = int(time.time())
        if start_ms is None:
            start_ms = (now - 900) * 1000
        if end_ms is None:
            end_ms = now * 1000
        start, end = start_ms // 1000, min(end_ms // 1000, now)
        start, end = (start // 10) * 10, (end // 10) * 10
        if end <= start or end - start > MAX_REQUEST_SECONDS:
            raise InvalidSelection("invalid time window")
        # Zabbix is the structural authority: Tempo outage must never
        # delete the topology or create fabricated NOT_REACHED stages.
        containers, attachments = self.zabbix.read()
        try:
            trace_view = self.tempo.view(trace_id, start, end)
            trace_status = (
                "OBSERVED" if trace_view.get("metadata") else "NO_TRACE_OBSERVED"
            )
        except Exception:
            trace_view = {}
            trace_status = "UNAVAILABLE"
        result = flow_map.build_flow_map(
            service_entities=SERVICE_ENTITIES,
            container_samples=containers,
            attachment_samples=attachments,
            expected_edges=EXPECTED_EDGES,
            trace_view=trace_view,
            network_events=(),  # No TCP correlation proof yet; do not invent.
            now_s=now,
            max_age_s=MAX_AGE_SECONDS,
            network_sensor_healthy=False,
        )
        result["source_status"] = {
            "zabbix": "OBSERVED" if containers else "NO_INVENTORY_EVIDENCE",
            "tempo": trace_status,
            "tcp_brain": "NOT_INTEGRATED",
        }
        result["evidence_completeness"] = (
            "PARTIAL" if trace_status != "OBSERVED" or not containers else
            "ZABBIX_AND_TRACE_OBSERVED"
        )
        return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log arbitrary URLs/IDs.

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                result = {"status": "ok", "purpose": "roc.flow-map.v1"}
            elif parsed.path == "/map":
                args = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(values) != 1 for values in args.values()):
                    raise InvalidSelection("ambiguous parameter")
                if not set(args) <= {"trace_id", "start", "end"}:
                    raise InvalidSelection("unsupported parameter")
                tid = args.get("trace_id", ["latest"])[0]
                start = int(args["start"][0]) if "start" in args else None
                end = int(args["end"][0]) if "end" in args else None
                result = self.server.projector.view(tid, start, end)
            else:
                self.send_error(404)
                return
            status = 200
        except (InvalidSelection, TypeError):
            result, status = {"error": "INVALID_SELECTION"}, 400
        except Exception:
            result, status = {"error": "OBSERVABILITY_SOURCE_UNAVAILABLE"}, 503
        body = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.projector = MapProjection()
    server.serve_forever()
