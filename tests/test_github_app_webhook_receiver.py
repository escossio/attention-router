import hashlib
import hmac
import importlib.util
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RECEIVER = ROOT / "ops/provisioning/github-app-control-plane/webhook_receiver.py"
INSTALLATION_ID = 162576919
SECRET = b"synthetic-webhook-secret-material-32-bytes"
HEAD = "a" * 40


def load_receiver(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    (credentials / "webhook-secret").write_bytes(SECRET + b"\n")

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", str(INSTALLATION_ID))
    monkeypatch.setenv("GITHUB_WEBHOOK_REPOSITORY", "escossio/attention-router")

    spec = importlib.util.spec_from_file_location("github_webhook_receiver_test", RECEIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.init_db()
    return module


@pytest.fixture
def receiver(monkeypatch, tmp_path):
    module = load_receiver(monkeypatch, tmp_path)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, *, payload, event, delivery, signed=True):
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }
    if signed:
        digest = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"

    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/github/webhook",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def pull_request_payload(*, repo="escossio/attention-router", installation=INSTALLATION_ID):
    return {
        "action": "opened",
        "number": 66,
        "repository": {"full_name": repo},
        "installation": {"id": installation},
        "pull_request": {"head": {"sha": HEAD}},
    }


def workflow_payload(
    *,
    name="Public CI",
    conclusion="success",
    head_sha=HEAD,
    pr_number=80,
    run_id=9001,
):
    return {
        "action": "completed",
        "repository": {"full_name": "escossio/attention-router"},
        "installation": {"id": INSTALLATION_ID},
        "workflow_run": {
            "id": run_id,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": head_sha,
            "pull_requests": [{"number": pr_number}],
        },
    }


def check_payload(
    *,
    name="postgres-integration",
    conclusion="success",
    head_sha=HEAD,
    pr_number=80,
    run_id=8001,
):
    return {
        "action": "completed",
        "repository": {"full_name": "escossio/attention-router"},
        "installation": {"id": INSTALLATION_ID},
        "check_run": {
            "id": run_id,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "head_sha": head_sha,
            "pull_requests": [{"number": pr_number}],
        },
    }


def test_unsigned_webhook_is_rejected(receiver):
    _, server = receiver
    status, body = request(
        server,
        payload={"zen": "synthetic"},
        event="ping",
        delivery="unsigned",
        signed=False,
    )
    assert status == 401
    assert body == {"error": "missing_signature"}


def test_signed_pull_request_is_persisted_once(receiver):
    module, server = receiver
    payload = pull_request_payload()

    status, body = request(
        server,
        payload=payload,
        event="pull_request",
        delivery="delivery-1",
    )
    assert status == 202
    assert body == {"status": "accepted"}

    status, body = request(
        server,
        payload=payload,
        event="pull_request",
        delivery="delivery-1",
    )
    assert status == 200
    assert body == {"status": "duplicate"}

    with sqlite3.connect(module.DB_PATH) as db:
        row = db.execute(
            "select event, action, repository, installation_id, pr_number, head_sha "
            "from deliveries where delivery_id = ?",
            ("delivery-1",),
        ).fetchone()

    assert row == (
        "pull_request",
        "opened",
        "escossio/attention-router",
        INSTALLATION_ID,
        66,
        HEAD,
    )


@pytest.mark.parametrize(
    ("repo", "installation", "error"),
    [
        ("escossio/not-attention-router", INSTALLATION_ID, "repository_not_allowed"),
        ("escossio/attention-router", INSTALLATION_ID + 1, "installation_mismatch"),
    ],
)
def test_repository_and_installation_are_fail_closed(receiver, repo, installation, error):
    _, server = receiver
    status, body = request(
        server,
        payload=pull_request_payload(repo=repo, installation=installation),
        event="pull_request",
        delivery=f"denied-{error}",
    )
    assert status == 403
    assert body == {"error": error}


def test_workflow_completion_transitions_exact_tracked_stage(receiver):
    module, server = receiver
    module.register_stage(
        "stage-workflow",
        "escossio/attention-router",
        80,
        HEAD,
        "workflow_run",
        "Public CI",
    )

    status, body = request(
        server,
        payload=workflow_payload(),
        event="workflow_run",
        delivery="workflow-success",
    )

    assert status == 202
    assert body == {
        "status": "accepted",
        "stage_transition": {
            "stage_id": "stage-workflow",
            "state": "COMPLETED_SUCCESS",
        },
    }
    stage = module.get_stage("stage-workflow")
    assert stage["state"] == "COMPLETED_SUCCESS"
    assert stage["run_id"] == 9001
    assert stage["conclusion"] == "success"

    signals = module.list_ready_signals()
    assert len(signals) == 1
    assert signals[0]["stage_id"] == "stage-workflow"
    assert signals[0]["state"] == "COMPLETED_SUCCESS"


def test_check_completion_can_signal_individual_postgres_gate(receiver):
    module, server = receiver
    module.register_stage(
        "stage-postgres",
        "escossio/attention-router",
        80,
        HEAD,
        "check_run",
        "postgres-integration",
    )

    status, body = request(
        server,
        payload=check_payload(),
        event="check_run",
        delivery="postgres-success",
    )

    assert status == 202
    assert body["stage_transition"] == {
        "stage_id": "stage-postgres",
        "state": "COMPLETED_SUCCESS",
    }
    stage = module.get_stage("stage-postgres")
    assert stage["state"] == "COMPLETED_SUCCESS"
    assert stage["run_id"] == 8001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "b" * 40),
        ("name", "docker-build"),
        ("pr_number", 81),
    ],
)
def test_stale_or_mismatched_completion_cannot_advance_stage(receiver, field, value):
    module, server = receiver
    module.register_stage(
        "stage-exact",
        "escossio/attention-router",
        80,
        HEAD,
        "check_run",
        "postgres-integration",
    )
    kwargs = {field: value}

    status, body = request(
        server,
        payload=check_payload(**kwargs),
        event="check_run",
        delivery=f"mismatch-{field}",
    )

    assert status == 202
    assert body == {"status": "accepted"}
    assert module.get_stage("stage-exact")["state"] == "RUNNING"
    assert module.list_ready_signals() == []


def test_duplicate_delivery_and_later_terminal_event_are_monotonic(receiver):
    module, server = receiver
    module.register_stage(
        "stage-once",
        "escossio/attention-router",
        80,
        HEAD,
        "check_run",
        "postgres-integration",
    )

    first = check_payload(conclusion="success", run_id=8002)
    status, body = request(
        server,
        payload=first,
        event="check_run",
        delivery="once",
    )
    assert status == 202
    assert body["stage_transition"]["state"] == "COMPLETED_SUCCESS"

    status, body = request(
        server,
        payload=first,
        event="check_run",
        delivery="once",
    )
    assert status == 200
    assert body == {"status": "duplicate"}

    status, body = request(
        server,
        payload=check_payload(conclusion="failure", run_id=8003),
        event="check_run",
        delivery="later-failure",
    )
    assert status == 202
    assert body == {"status": "accepted"}

    stage = module.get_stage("stage-once")
    assert stage["state"] == "COMPLETED_SUCCESS"
    assert stage["run_id"] == 8002
    assert len(module.list_ready_signals()) == 1


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("failure", "COMPLETED_FAILURE"),
        ("timed_out", "COMPLETED_FAILURE"),
        ("cancelled", "CANCELLED"),
    ],
)
def test_terminal_conclusions_map_to_bounded_stage_states(receiver, conclusion, expected):
    module, server = receiver
    stage_id = f"stage-{conclusion}"
    module.register_stage(
        stage_id,
        "escossio/attention-router",
        80,
        HEAD,
        "workflow_run",
        "Public CI",
    )
    status, body = request(
        server,
        payload=workflow_payload(conclusion=conclusion, run_id=9100),
        event="workflow_run",
        delivery=f"terminal-{conclusion}",
    )
    assert status == 202
    assert body["stage_transition"]["state"] == expected
    assert module.get_stage(stage_id)["state"] == expected


def test_ready_signal_can_be_acknowledged_once(receiver):
    module, server = receiver
    module.register_stage(
        "stage-ack",
        "escossio/attention-router",
        80,
        HEAD,
        "check_run",
        "python-tests",
    )
    request(
        server,
        payload=check_payload(name="python-tests", run_id=8200),
        event="check_run",
        delivery="ack-ready",
    )

    assert len(module.list_ready_signals()) == 1
    assert module.acknowledge_signal("stage-ack") is True
    assert module.list_ready_signals() == []
    assert module.acknowledge_signal("stage-ack") is False


def test_existing_delivery_database_is_migrated_for_check_run_fields(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir()
    state.mkdir()
    (credentials / "webhook-secret").write_bytes(SECRET + b"\n")
    old_db = state / "events.sqlite3"
    with sqlite3.connect(old_db) as db:
        db.execute("""CREATE TABLE deliveries (
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

    module = load_receiver(monkeypatch, tmp_path)
    with sqlite3.connect(module.DB_PATH) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}

    assert {"check_run_id", "check_name", "check_status", "check_conclusion"} <= columns
