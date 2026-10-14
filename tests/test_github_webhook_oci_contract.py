"""Webhook OCI contract: portable, isolated from GitHub App token publisher."""
import importlib.util
import sqlite3
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "ops/provisioning/github-app-control-plane/container"
CFG = yaml.safe_load((CANDIDATE / "compose.yaml").read_text())


def test_webhook_image_is_pinned_and_does_not_bundle_secrets():
    dockerfile = (CANDIDATE / "Dockerfile").read_text()
    assert dockerfile.startswith("FROM python:3.13-alpine@sha256:")
    assert "USER 10001:10001" in dockerfile
    assert "COPY webhook_receiver.py" in dockerfile
    assert "private-key.pem" not in dockerfile
    assert "CREDENTIALS_DIRECTORY=/run/webhook-credentials" in dockerfile
    assert (CANDIDATE / "webhook_receiver.py").read_bytes() == (
        ROOT / "ops/provisioning/github-app-control-plane/webhook_receiver.py"
    ).read_bytes()


def test_webhook_mounts_state_and_only_one_hmac_secret():
    c = CFG["services"]["webhook"]
    assert CFG["name"] == "attention-router-github-control"
    assert c["user"] == "10001:10001"
    assert c["read_only"] is True
    assert c["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in c["security_opt"]
    assert c["restart"] == "unless-stopped"
    assert "healthcheck" in c
    assert "privileged" not in c and "network_mode" not in c
    assert c["ports"][0].startswith("${GITHUB_WEBHOOK_BIND_HOST:-127.0.0.1}")
    assert c["ports"][0].endswith(":18104")
    mounts = {v["target"]: v for v in c["volumes"]}
    assert set(mounts) == {"/state", "/run/webhook-credentials"}
    assert mounts["/run/webhook-credentials"]["read_only"] is True
    assert "GITHUB_WEBHOOK_SECRET_DIR" in mounts["/run/webhook-credentials"]["source"]
    assert "GITHUB_WEBHOOK_DB_DIR" in mounts["/state"]["source"]
    assert not mounts["/state"].get("read_only", False)
    assert "GITHUB_WEBHOOK_REPOSITORIES" in c["environment"]
    assert "GITHUB_APP_INSTALLATION_ID" in c["environment"]
    assert "GITHUB_APP_PRIVATE_KEY_PATH" not in c["environment"]


def test_sqlite_delivery_is_idempotent_and_stage_transition_is_monotonic(tmp_path):
    spec = importlib.util.spec_from_file_location("webhook_candidate", CANDIDATE / "webhook_receiver.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_DIR = tmp_path
    module.DB_PATH = tmp_path / "events.sqlite3"
    module.init_db()
    sha = "a" * 40
    module.register_stage("stage-1", "escossio/attention-router", 10, sha, "workflow_run", "transport-tests")
    payload = {
        "repository": {"full_name": "escossio/attention-router"},
        "installation": {"id": 10001},
        "action": "completed",
        "workflow_run": {"id": 77, "name": "transport-tests", "status": "completed",
                         "conclusion": "success", "head_sha": sha,
                         "pull_requests": [{"number": 10}]},
    }
    meta = module.extract_metadata("workflow_run", payload)
    created, transition = module.process_delivery("delivery-1", "workflow_run", meta)
    assert created is True
    assert transition == {"stage_id": "stage-1", "state": "COMPLETED_SUCCESS"}
    created_again, transition_again = module.process_delivery("delivery-1", "workflow_run", meta)
    assert created_again is False
    assert transition_again is None
    signals = module.list_ready_signals()
    assert len(signals) == 1
    assert signals[0]["state"] == "COMPLETED_SUCCESS"
    assert module.acknowledge_signal("stage-1") is True
    assert module.acknowledge_signal("stage-1") is False
    with sqlite3.connect(module.DB_PATH) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


def test_allowlist_and_hmac_are_preserved_in_candidate_source():
    s = (CANDIDATE / "webhook_receiver.py").read_text()
    assert "hmac.compare_digest" in s
    assert "meta[\"repository\"] not in TARGET_REPOS" in s
    assert "meta[\"installation_id\"] != INSTALLATION_ID" in s
    assert "return self.json_response(401" in s
    assert "return self.json_response(403" in s
    assert "return self.json_response(413" in s
    assert "GITHUB_APP_PRIVATE_KEY_PATH" not in s
    assert "subprocess" not in s
