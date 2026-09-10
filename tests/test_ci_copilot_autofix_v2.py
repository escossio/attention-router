from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from scripts import ci_copilot_autofix_persist as persist
from scripts import ci_copilot_autofix_proposal as proposal
from scripts import ci_copilot_patch_trial as trial


WORKFLOW = Path(".github/workflows/ci-agent.yml")
PROPOSAL_SCRIPT = Path("scripts/ci_copilot_autofix_proposal.py")
PERSIST_SCRIPT = Path("scripts/ci_copilot_autofix_persist.py")


def _patch() -> str:
    return """--- a/tests/test_example.py
+++ b/tests/test_example.py
@@ -1,2 +1,2 @@
 def test_example():
-    assert False
+    assert True
"""


def _artifact() -> dict[str, object]:
    patch = _patch()
    return {
        "schema": proposal.AUTOFIX_PROPOSAL_SCHEMA,
        "repo": "escossio/attention-router",
        "pr_number": 99,
        "source_head": "a" * 40,
        "branch": "ci-agent-autofix/proof",
        "trigger_workflow": "Public CI",
        "trigger_run_id": 123,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "log_excerpt": "FAILED tests/test_example.py::test_example",
        "patch_proposal": {
            "schema": trial.PATCH_SCHEMA,
            "decision": "PATCH",
            "confidence": "HIGH",
            "summary": "Remove the controlled failure.",
            "target_files": ["tests/test_example.py"],
            "patch": patch,
        },
    }


def test_persistent_attempt_budget_is_one_and_detects_persisted_comment(monkeypatch):
    monkeypatch.setattr(
        proposal,
        "_comments",
        lambda *args, **kwargs: [
            {"body": proposal.AUTOFIX_MARKER + "\n**State:** `PERSISTED`", "user": {"type": "Bot"}}
        ],
    )

    assert proposal.MAX_PERSISTENT_ATTEMPTS == 1
    assert proposal.persistent_attempt_used("escossio/attention-router", 99, token="x") is True


def test_branch_guard_requires_explicit_autofix_prefix():
    persist._validate_branch_name("ci-agent-autofix/proof")

    for branch in ("feature/proof", "ci-agent-autofix/../main", "ci-agent-autofix//proof"):
        with pytest.raises(persist.AutofixPersistError):
            persist._validate_branch_name(branch)


def test_artifact_round_trip_and_contract_validation():
    artifact = _artifact()
    encoded = base64.urlsafe_b64encode(json.dumps(artifact).encode()).decode()

    decoded = persist._decode_artifact(encoded)
    patch, log_excerpt = persist._validate_artifact(
        decoded,
        repo="escossio/attention-router",
        pr_number=99,
        source_head="a" * 40,
        branch="ci-agent-autofix/proof",
        changed_files=("tests/test_example.py",),
    )

    assert patch.decision == "PATCH"
    assert patch.confidence == "HIGH"
    assert patch.target_files == ("tests/test_example.py",)
    assert log_excerpt.startswith("FAILED tests/test_example.py")


def test_artifact_rejects_hash_or_source_scope_change():
    artifact = _artifact()
    artifact["patch_sha256"] = "0" * 64
    with pytest.raises(persist.AutofixPersistError, match="hash mismatch"):
        persist._validate_artifact(
            artifact,
            repo="escossio/attention-router",
            pr_number=99,
            source_head="a" * 40,
            branch="ci-agent-autofix/proof",
            changed_files=("tests/test_example.py",),
        )

    artifact = _artifact()
    with pytest.raises(trial.PatchTrialError):
        persist._validate_artifact(
            artifact,
            repo="escossio/attention-router",
            pr_number=99,
            source_head="a" * 40,
            branch="ci-agent-autofix/proof",
            changed_files=("attention_router/core/authority.py",),
        )


def test_git_data_persistence_is_one_commit_and_non_force(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, url, *, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET" and "/git/commits/" in url:
            return {"tree": {"sha": "base-tree"}}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "blob-sha"}
        if method == "POST" and url.endswith("/git/trees"):
            return {"sha": "tree-sha"}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "agent-commit"}
        if method == "PATCH" and "/git/refs/heads/" in url:
            return {"object": {"sha": "agent-commit"}}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(persist.ci_agent, "_request_json", fake_request)

    commit = persist._create_commit_from_contents(
        "escossio/attention-router",
        source_head="a" * 40,
        branch="ci-agent-autofix/proof",
        contents={"tests/test_example.py": "def test_example():\n    assert True\n"},
        modes={"tests/test_example.py": "100644"},
        patch_sha256="f" * 64,
        token="token",
    )

    assert commit == "agent-commit"
    commit_calls = [call for call in calls if call[0] == "POST" and call[1].endswith("/git/commits")]
    assert len(commit_calls) == 1
    assert commit_calls[0][2]["parents"] == ["a" * 40]
    ref_call = next(call for call in calls if call[0] == "PATCH")
    assert ref_call[2] == {"sha": "agent-commit", "force": False}


def test_workflow_separates_copilot_from_repository_write_authority():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "cancel-in-progress: false" in workflow
    assert "autofix-proposal:" in workflow
    assert "autofix-persist:" in workflow
    assert "startsWith(github.event.workflow_run.head_branch, 'ci-agent-autofix/')" in workflow
    assert workflow.count("contents: write") == 1

    proposal_job = workflow.split("  autofix-proposal:", 1)[1].split("  autofix-persist:", 1)[0]
    persist_job = workflow.split("  autofix-persist:", 1)[1]
    assert "contents: read" in proposal_job
    assert "copilot-requests: write" in proposal_job
    assert "contents: write" not in proposal_job
    assert "contents: write" in persist_job
    assert "copilot-requests: write" not in persist_job
    assert "npm install -g @github/copilot" not in persist_job
    assert "persist-credentials: false" in persist_job
    assert "python -m scripts.ci_copilot_autofix_persist" in persist_job


def test_persistence_uses_no_git_push_or_shell_commit():
    source = PERSIST_SCRIPT.read_text(encoding="utf-8")
    proposal_source = PROPOSAL_SCRIPT.read_text(encoding="utf-8")

    assert '"git", "push"' not in source
    assert '"git", "commit"' not in source
    assert "git push" not in source
    assert "git commit" not in source
    assert "CI-Agent-Merge: disabled" in source
    assert "contents: write" not in proposal_source


def test_controlled_v2_persistent_autofix_proof():
    assert True
