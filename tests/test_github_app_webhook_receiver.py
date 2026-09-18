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


def load_receiver(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir()
    state.mkdir()
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
        "pull_request": {"head": {"sha": "a" * 40}},
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
        "a" * 40,
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
