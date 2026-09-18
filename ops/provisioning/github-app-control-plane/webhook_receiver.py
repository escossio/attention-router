#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("GITHUB_WEBHOOK_HOST", "127.0.0.1")
PORT = int(os.environ.get("GITHUB_WEBHOOK_PORT", "18104"))
TARGET_REPO = os.environ.get("GITHUB_WEBHOOK_REPOSITORY", "escossio/attention-router")
INSTALLATION_ID = int(os.environ.get("GITHUB_APP_INSTALLATION_ID", "0"))
MAX_BODY = int(os.environ.get("GITHUB_WEBHOOK_MAX_BODY", str(1024 * 1024)))
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/andy-github-app"))
DB_PATH = STATE_DIR / "events.sqlite3"
ALLOWED_EVENTS = {"ping", "pull_request", "workflow_run"}

def webhook_secret():
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    value = (credentials / "webhook-secret").read_text().strip()
    if not value:
        raise RuntimeError("empty webhook secret")
    return value.encode()

def init_db():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            event TEXT NOT NULL,
            action TEXT,
            repository TEXT,
            installation_id INTEGER,
            pr_number INTEGER,
            head_sha TEXT,
            workflow_run_id INTEGER,
            workflow_name TEXT,
            workflow_status TEXT,
            workflow_conclusion TEXT
        )""")

def extract_metadata(event, payload):
    repo = ((payload.get("repository") or {}).get("full_name"))
    installation = ((payload.get("installation") or {}).get("id"))
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    workflow = payload.get("workflow_run") or {}
    head_sha = pr.get("head", {}).get("sha") if pr else None
    if not head_sha:
        head_sha = workflow.get("head_sha")
    return {
        "action": action,
        "repository": repo,
        "installation_id": installation,
        "pr_number": ((payload.get("number") if event == "pull_request" else None)
                      or ((workflow.get("pull_requests") or [{}])[0].get("number")
                          if workflow.get("pull_requests") else None)),
        "head_sha": head_sha,
        "workflow_run_id": workflow.get("id"),
        "workflow_name": workflow.get("name"),
        "workflow_status": workflow.get("status"),
        "workflow_conclusion": workflow.get("conclusion"),
    }

def record_delivery(delivery_id, event, meta):
    row = (
        delivery_id,
        datetime.now(timezone.utc).isoformat(),
        event,
        meta["action"],
        meta["repository"],
        meta["installation_id"],
        meta["pr_number"],
        meta["head_sha"],
        meta["workflow_run_id"],
        meta["workflow_name"],
        meta["workflow_status"],
        meta["workflow_conclusion"],
    )
    try:
        with sqlite3.connect(DB_PATH) as db:
            db.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        return True
    except sqlite3.IntegrityError:
        return False

class Handler(BaseHTTPRequestHandler):
    server_version = "AndyGitHubWebhook/0.1"

    def log_message(self, fmt, *args):
        return

    def json_response(self, status, body):
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self.json_response(200, {"status": "ok"})
        return self.json_response(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/github/webhook":
            return self.json_response(404, {"error": "not_found"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.json_response(400, {"error": "invalid_length"})
        if length <= 0 or length > MAX_BODY:
            return self.json_response(413, {"error": "invalid_body_size"})

        body = self.rfile.read(length)
        signature = self.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return self.json_response(401, {"error": "missing_signature"})

        expected = "sha256=" + hmac.new(
            webhook_secret(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return self.json_response(401, {"error": "invalid_signature"})

        event = self.headers.get("X-GitHub-Event", "")
        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        if event not in ALLOWED_EVENTS:
            return self.json_response(202, {"status": "ignored_event"})
        if not delivery_id:
            return self.json_response(400, {"error": "missing_delivery_id"})

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return self.json_response(400, {"error": "invalid_json"})

        meta = extract_metadata(event, payload)
        if event != "ping":
            if meta["repository"] != TARGET_REPO:
                return self.json_response(403, {"error": "repository_not_allowed"})
            if INSTALLATION_ID and meta["installation_id"] != INSTALLATION_ID:
                return self.json_response(403, {"error": "installation_mismatch"})

        created = record_delivery(delivery_id, event, meta)
        return self.json_response(
            202 if created else 200,
            {"status": "accepted" if created else "duplicate"},
        )

def main():
    init_db()
    secret = webhook_secret()
    if len(secret) < 32:
        raise RuntimeError("webhook secret too short")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LISTENING={HOST}:{PORT}", flush=True)
    print(f"TARGET_REPOSITORY={TARGET_REPO}", flush=True)
    print("MODE=OBSERVATION_ONLY", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
