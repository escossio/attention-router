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
ALLOWED_EVENTS = {"ping", "pull_request", "workflow_run", "check_run"}
TERMINAL_STATES = {"COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CANCELLED"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def webhook_secret():
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    value = (credentials / "webhook-secret").read_text().strip()
    if not value:
        raise RuntimeError("empty webhook secret")
    return value.encode()


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _ensure_column(db, table, definition):
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


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
        _ensure_column(db, "deliveries", "check_run_id INTEGER")
        _ensure_column(db, "deliveries", "check_name TEXT")
        _ensure_column(db, "deliveries", "check_status TEXT")
        _ensure_column(db, "deliveries", "check_conclusion TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS tracked_stages (
            stage_id TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            target_name TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_delivery_id TEXT,
            run_id INTEGER,
            conclusion TEXT,
            UNIQUE(repository, pr_number, head_sha, event_kind, target_name),
            CHECK(event_kind IN ('workflow_run','check_run')),
            CHECK(state IN ('RUNNING','COMPLETED_SUCCESS','COMPLETED_FAILURE','CANCELLED'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS continuation_signals (
            signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage_id TEXT NOT NULL UNIQUE,
            emitted_at TEXT NOT NULL,
            state TEXT NOT NULL,
            consumed_at TEXT,
            FOREIGN KEY(stage_id) REFERENCES tracked_stages(stage_id)
        )""")


def extract_metadata(event, payload):
    repo = (payload.get("repository") or {}).get("full_name")
    installation = (payload.get("installation") or {}).get("id")
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    workflow = payload.get("workflow_run") or {}
    check = payload.get("check_run") or {}

    head_sha = pr.get("head", {}).get("sha") if pr else None
    if not head_sha:
        head_sha = workflow.get("head_sha")
    if not head_sha:
        head_sha = check.get("head_sha")

    pr_number = payload.get("number") if event == "pull_request" else None
    if event == "workflow_run" and workflow.get("pull_requests"):
        pr_number = (workflow.get("pull_requests") or [{}])[0].get("number")
    if event == "check_run" and check.get("pull_requests"):
        pr_number = (check.get("pull_requests") or [{}])[0].get("number")

    return {
        "action": action,
        "repository": repo,
        "installation_id": installation,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "workflow_run_id": workflow.get("id"),
        "workflow_name": workflow.get("name"),
        "workflow_status": workflow.get("status"),
        "workflow_conclusion": workflow.get("conclusion"),
        "check_run_id": check.get("id"),
        "check_name": check.get("name"),
        "check_status": check.get("status"),
        "check_conclusion": check.get("conclusion"),
    }


def _completion_projection(event, meta):
    if event == "workflow_run":
        return (
            meta["workflow_run_id"],
            meta["workflow_name"],
            meta["workflow_status"],
            meta["workflow_conclusion"],
        )
    if event == "check_run":
        return (
            meta["check_run_id"],
            meta["check_name"],
            meta["check_status"],
            meta["check_conclusion"],
        )
    return None


def _terminal_state(conclusion):
    if conclusion == "success":
        return "COMPLETED_SUCCESS"
    if conclusion == "cancelled":
        return "CANCELLED"
    return "COMPLETED_FAILURE"


def register_stage(stage_id, repository, pr_number, head_sha, event_kind, target_name):
    if event_kind not in {"workflow_run", "check_run"}:
        raise ValueError("unsupported event kind")
    if len(head_sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in head_sha):
        raise ValueError("head_sha must be 40 hexadecimal characters")
    if not target_name.strip():
        raise ValueError("target_name is required")
    stamp = now_iso()
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO tracked_stages (
                stage_id, repository, pr_number, head_sha, event_kind, target_name,
                state, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,'RUNNING',?,?)""",
            (stage_id, repository, pr_number, head_sha.lower(), event_kind, target_name, stamp, stamp),
        )


def process_delivery(delivery_id, event, meta):
    received_at = now_iso()
    row = (
        delivery_id,
        received_at,
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
        meta["check_run_id"],
        meta["check_name"],
        meta["check_status"],
        meta["check_conclusion"],
    )
    try:
        with sqlite3.connect(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO deliveries (
                    delivery_id, received_at, event, action, repository, installation_id,
                    pr_number, head_sha, workflow_run_id, workflow_name, workflow_status,
                    workflow_conclusion, check_run_id, check_name, check_status, check_conclusion
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )

            projection = _completion_projection(event, meta)
            transitioned = None
            if (
                projection is not None
                and meta["action"] == "completed"
                and projection[2] == "completed"
                and meta["pr_number"] is not None
                and meta["head_sha"]
                and projection[1]
            ):
                run_id, target_name, _, conclusion = projection
                state = _terminal_state(conclusion)
                stage = db.execute(
                    """SELECT stage_id FROM tracked_stages
                       WHERE repository = ? AND pr_number = ? AND head_sha = ?
                         AND event_kind = ? AND target_name = ? AND state = 'RUNNING'""",
                    (
                        meta["repository"],
                        meta["pr_number"],
                        meta["head_sha"].lower(),
                        event,
                        target_name,
                    ),
                ).fetchone()
                if stage is not None:
                    stage_id = stage[0]
                    cur = db.execute(
                        """UPDATE tracked_stages
                           SET state = ?, updated_at = ?, completed_delivery_id = ?,
                               run_id = ?, conclusion = ?
                           WHERE stage_id = ? AND state = 'RUNNING'""",
                        (state, received_at, delivery_id, run_id, conclusion, stage_id),
                    )
                    if cur.rowcount == 1:
                        db.execute(
                            """INSERT INTO continuation_signals(stage_id, emitted_at, state)
                               VALUES (?,?,?)""",
                            (stage_id, received_at, state),
                        )
                        transitioned = {"stage_id": stage_id, "state": state}
            db.commit()
        return True, transitioned
    except sqlite3.IntegrityError as exc:
        if "deliveries.delivery_id" in str(exc) or "UNIQUE constraint failed: deliveries.delivery_id" in str(exc):
            return False, None
        raise


def get_stage(stage_id):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM tracked_stages WHERE stage_id = ?",
            (stage_id,),
        ).fetchone()
        return dict(row) if row else None


def list_ready_signals():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT s.signal_id, s.stage_id, s.emitted_at, s.state,
                      t.repository, t.pr_number, t.head_sha, t.event_kind,
                      t.target_name, t.run_id, t.conclusion
               FROM continuation_signals s
               JOIN tracked_stages t ON t.stage_id = s.stage_id
               WHERE s.consumed_at IS NULL
               ORDER BY s.signal_id"""
        ).fetchall()
        return [dict(row) for row in rows]


def acknowledge_signal(stage_id):
    stamp = now_iso()
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            """UPDATE continuation_signals
               SET consumed_at = ?
               WHERE stage_id = ? AND consumed_at IS NULL""",
            (stamp, stage_id),
        )
        return cur.rowcount == 1


class Handler(BaseHTTPRequestHandler):
    server_version = "AndyGitHubWebhook/0.2"

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

        created, transitioned = process_delivery(delivery_id, event, meta)
        response = {"status": "accepted" if created else "duplicate"}
        if transitioned is not None:
            response["stage_transition"] = transitioned
        return self.json_response(202 if created else 200, response)


def main():
    init_db()
    secret = webhook_secret()
    if len(secret) < 32:
        raise RuntimeError("webhook secret too short")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LISTENING={HOST}:{PORT}", flush=True)
    print(f"TARGET_REPOSITORY={TARGET_REPO}", flush=True)
    print("MODE=EVENT_DRIVEN_STAGE_SIGNAL", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
