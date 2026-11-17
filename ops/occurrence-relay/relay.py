"""Minimal Zabbix occurrence relay: signed admission, durable numbering, reliable outbox.

No prioritization, AI, ticket UI, or autonomous investigation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY = 16384
ALLOWED = frozenset({
    "schema_version", "event_id", "problem_event_id", "occurred_at",
    "asset_ref", "service_ref", "interface_ref", "signal_code", "status",
    "severity", "summary",
})
REQUIRED = frozenset({
    "schema_version", "event_id", "occurred_at", "asset_ref", "signal_code", "status",
})


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def validate(raw: bytes) -> tuple[dict, str]:
    if len(raw) > MAX_BODY:
        raise ValueError("too large")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(data, dict) or not REQUIRED.issubset(data) or set(data) - ALLOWED:
        raise ValueError("invalid fields")
    if data["schema_version"] != "1" or data["status"] not in ("PROBLEM", "RECOVERY", "UPDATE"):
        raise ValueError("invalid version or status")
    for key in ("event_id", "asset_ref", "signal_code"):
        val = data[key]
        if type(val) is not str or not 1 <= len(val) <= 160 or not val.strip():
            raise ValueError("invalid " + key)
    for key in ("problem_event_id", "service_ref", "interface_ref", "summary"):
        if key in data and (type(data[key]) is not str or not 1 <= len(data[key]) <= 500):
            raise ValueError("invalid " + key)
    if data["status"] == "RECOVERY" and not data.get("problem_event_id"):
        raise ValueError("recovery needs problem_event_id")
    if "severity" in data and (type(data["severity"]) is not int or not 0 <= data["severity"] <= 5):
        raise ValueError("invalid severity")
    stamp = data["occurred_at"]
    if type(stamp) is not str or len(stamp) > 45:
        raise ValueError("invalid timestamp")
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if t.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    # Hash canonical accepted facts: retries with only JSON whitespace differences are identical.
    canonical = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return data, hashlib.sha256(canonical.encode("ascii")).hexdigest()


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _init(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS delivery (
                number INTEGER PRIMARY KEY REFERENCES events(number),
                delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            )""")

    def admit(self, data: dict, digest: str) -> tuple[int, bool]:
        raw = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT number,content_hash FROM events WHERE source_event_id=?", (data["event_id"],)
            ).fetchone()
            if old:
                if not hmac.compare_digest(old[1], digest):
                    raise ValueError("event id content conflict")
                return old[0], False
            cur = db.execute(
                "INSERT INTO events(source_event_id,content_hash,state,occurred_at,payload) VALUES (?,?,?,?,?)",
                (data["event_id"], digest, data["status"], data["occurred_at"], raw),
            )
            number = cur.lastrowid
            db.execute("INSERT INTO delivery(number) VALUES (?)", (number,))
            return number, True

    def next_delivery(self):
        with self._connect() as db:
            return db.execute(
                "SELECT e.number,e.payload FROM events e JOIN delivery d USING(number)"
                " WHERE d.delivered=0 ORDER BY e.number LIMIT 1"
            ).fetchone()

    def delivered(self, number: int):
        with self._connect() as db:
            db.execute("UPDATE delivery SET delivered=1,attempts=attempts+1 WHERE number=?", (number,))

    def retry(self, number: int):
        with self._connect() as db:
            db.execute("UPDATE delivery SET attempts=attempts+1 WHERE number=?", (number,))

    def counts(self):
        with self._connect() as db:
            return {
                "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "pending_delivery": db.execute("SELECT COUNT(*) FROM delivery WHERE delivered=0").fetchone()[0],
            }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class App:
    def __init__(self, store: Store, inbound_token: str, tenant: str, source: str,
                 maestro_url: str = "", maestro_token: str = ""):
        self.store = store
        self.inbound_token = inbound_token
        self.tenant = tenant
        self.source = source
        self.maestro_url = maestro_url
        self.maestro_token = maestro_token
        if maestro_url and (not maestro_url.startswith("https://") or not maestro_token):
            raise ValueError("maestro delivery requires HTTPS URL and token")

    def dispatch_one(self) -> bool:
        if not self.maestro_url:
            return False
        found = self.store.next_delivery()
        if found is None:
            return False
        number, payload = found
        envelope = {
            "contract_version": "occurrence-relay-v0-draft",
            "occurrence_number": number,
            "source_system": self.source,
            "tenant_id": self.tenant,
            "signal": json.loads(payload),
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.maestro_url,
            body,
            headers={"Authorization": "Bearer " + self.maestro_token,
                     "Content-Type": "application/json",
                     "Idempotency-Key": f"{self.source}:{number}"},
            method="POST",
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=4) as response:
                if not 200 <= response.status < 300:
                    self.store.retry(number)
                    return False
        except (HTTPError, URLError, TimeoutError, OSError):
            self.store.retry(number)
            return False
        self.store.delivered(number)
        return True


def handler_factory(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OccurrenceRelay/0"
        def _reply(self, status: int, payload: dict):
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"status": "UP"})
            elif self.path == "/status":
                self._reply(200, {"status": "UP", "maestro_configured": bool(app.maestro_url),
                                  **app.store.counts()})
            else:
                self._reply(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if self.path != "/v1/zabbix/events":
                self._reply(404, {"error": "NOT_FOUND"})
                return
            # The stdlib HTTP server does not reject ambiguous framing by itself.
            # Never trust the first of two lengths or a body marked chunked.
            lengths = self.headers.get_all("Content-Length", [])
            authorizations = self.headers.get_all("Authorization", [])
            contents = self.headers.get_all("Content-Type", [])
            if (
                len(lengths) != 1
                or len(authorizations) > 1
                or len(contents) != 1
                or contents[0].lower() not in ("application/json", "application/json; charset=utf-8")
                or self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Content-Encoding")
            ):
                self.close_connection = True
                self._reply(400, {"error": "INVALID_HTTP_ENVELOPE"})
                return
            provided = authorizations[0] if authorizations else ""
            if not hmac.compare_digest(provided, "Bearer " + app.inbound_token):
                self._reply(401, {"error": "UNAUTHORIZED"})
                return
            length = self.headers.get("Content-Length")
            if not length or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
                self._reply(413, {"error": "INVALID_LENGTH"})
                return
            try:
                data, digest = validate(self.rfile.read(int(length)))
                number, created = app.store.admit(data, digest)
            except (ValueError, UnicodeError, json.JSONDecodeError):
                self._reply(422, {"error": "INVALID_EVENT_OR_CONFLICT"})
                return
            except sqlite3.Error:
                self._reply(503, {"error": "STORAGE_UNAVAILABLE"})
                return
            self._reply(201 if created else 200, {"occurrence_number": number,
                                                   "status": "ACCEPTED" if created else "DUPLICATE"})

        def log_message(self, *_args):
            # No event contents or incoming tokens in application logs.
            pass
    return Handler


def run():
    token_path = os.environ["ZABBIX_INGRESS_TOKEN_FILE"]
    token = Path(token_path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("ingress token too short")
    maestro_url = os.getenv("MAESTRO_URL", "")
    maestro_token = ""
    if maestro_url:
        maestro_token = Path(os.environ["MAESTRO_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    app = App(Store(os.environ.get("RELAY_DB_PATH", "/data/relay.sqlite3")),
              token, os.environ["RELAY_TENANT"], os.environ.get("RELAY_SOURCE", "zabbix"),
              maestro_url, maestro_token)
    if maestro_url:
        def dispatch_loop():
            while True:
                if not app.dispatch_one():
                    time.sleep(3)
        threading.Thread(target=dispatch_loop, daemon=True).start()
    server = ThreadingHTTPServer((os.getenv("RELAY_BIND", "0.0.0.0"),
                                  int(os.getenv("RELAY_PORT", "8080"))), handler_factory(app))
    server.serve_forever()


if __name__ == "__main__":
    run()
