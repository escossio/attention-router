import subprocess
from pathlib import Path

import pytest

from scripts import ci_agent
from scripts import ci_copilot_patch_trial as trial


WORKFLOW = Path(".github/workflows/ci-agent.yml")
SCRIPT = Path("scripts/ci_copilot_patch_trial.py")


def _failure(category: str = "TEST_FAILURE") -> ci_agent.FailedStep:
    return ci_agent.FailedStep(
        workflow="Public CI",
        job="python-tests",
        step="Run python -m pytest -q",
        category=category,
        auto_fix_eligible=False,
    )


def _valid_patch() -> str:
    return """--- a/tests/test_example.py
+++ b/tests/test_example.py
@@ -1,2 +1,2 @@
 def test_example():
-    assert False
+    assert True
"""


def test_patchable_files_are_existing_python_tests_only():
    changed = (
        "tests/test_example.py",
        "attention_router/core/authority.py",
        ".github/workflows/ci.yml",
        "tests/../attention_router/core.py",
        "tests/data.json",
    )

    assert trial.patchable_changed_files(changed) == ("tests/test_example.py",)


def test_patch_trial_requires_explicit_branch_opt_in_and_safe_category():
    changed = ("tests/test_example.py",)

    assert trial._policy_block_reason(
        branch="feature/normal",
        failures=(_failure(),),
        changed_files=changed,
    ) == "BRANCH_NOT_OPTED_IN"
    assert trial._policy_block_reason(
        branch="ci-agent-patch-trial/proof",
        failures=(_failure("SECURITY_ANALYSIS_FAILURE"),),
        changed_files=changed,
    ) == "CATEGORY_NOT_PATCH_TRIAL_ALLOWLISTED"
    assert trial._policy_block_reason(
        branch="ci-agent-patch-trial/proof",
        failures=(_failure(),),
        changed_files=changed,
    ) is None


def test_valid_high_confidence_test_patch_is_accepted():
    payload = {
        "schema": trial.PATCH_SCHEMA,
        "decision": "PATCH",
        "confidence": "HIGH",
        "summary": "Remove the intentional synthetic failure.",
        "target_files": ["tests/test_example.py"],
        "patch": _valid_patch(),
    }

    proposal = trial.validate_patch_payload(payload, patchable_files=("tests/test_example.py",))

    assert proposal.decision == "PATCH"
    assert proposal.target_files == ("tests/test_example.py",)
    assert trial._changed_line_count(proposal.patch) == 2


def test_patch_rejects_source_file_low_confidence_and_file_operations():
    base = {
        "schema": trial.PATCH_SCHEMA,
        "decision": "PATCH",
        "confidence": "HIGH",
        "summary": "Synthetic diagnosis.",
        "target_files": ["attention_router/core/authority.py"],
        "patch": "--- a/attention_router/core/authority.py\n+++ b/attention_router/core/authority.py\n@@ -1 +1 @@\n-a\n+b\n",
    }
    with pytest.raises(trial.PatchTrialError, match="outside the V1.5 test allowlist"):
        trial.validate_patch_payload(base, patchable_files=("tests/test_example.py",))

    low = {
        **base,
        "confidence": "MEDIUM",
        "target_files": ["tests/test_example.py"],
        "patch": _valid_patch(),
    }
    with pytest.raises(trial.PatchTrialError, match="HIGH confidence"):
        trial.validate_patch_payload(low, patchable_files=("tests/test_example.py",))

    file_create = {
        **low,
        "confidence": "HIGH",
        "patch": "new file mode 100644\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -0,0 +1 @@\n+pass\n",
    }
    with pytest.raises(trial.PatchTrialError, match="forbidden file operation"):
        trial.validate_patch_payload(file_create, patchable_files=("tests/test_example.py",))


def test_non_patch_decision_cannot_smuggle_diff():
    payload = {
        "schema": trial.PATCH_SCHEMA,
        "decision": "NEEDS_HUMAN",
        "confidence": "LOW",
        "summary": "Needs review.",
        "target_files": ["tests/test_example.py"],
        "patch": _valid_patch(),
    }

    with pytest.raises(trial.PatchTrialError, match="must not contain patch"):
        trial.validate_patch_payload(payload, patchable_files=("tests/test_example.py",))


def test_validation_environment_strips_github_and_oidc_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "secret")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "secret")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.invalid")

    env = trial._validation_env()

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "COPILOT_GITHUB_TOKEN" not in env
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in env
    assert "ACTIONS_ID_TOKEN_REQUEST_URL" not in env
    assert env["CI_COPILOT_PATCH_TRIAL"] == "1"


def test_failed_validation_reports_cleanup_after_finally(monkeypatch, tmp_path):
    proposal = trial.PatchProposal(
        decision="PATCH",
        confidence="HIGH",
        summary="Synthetic patch.",
        target_files=("tests/test_example.py",),
        patch=_valid_patch(),
    )
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "tests/test_example.py\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "lint failed", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )

    def fake_run(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(trial, "_run", fake_run)

    result = trial.apply_and_validate_ephemerally(
        proposal,
        candidate_dir=tmp_path,
        log_excerpt="FAILED tests/test_example.py::test_example",
    )

    assert result.status == "VALIDATION_FAILED"
    assert result.cleanup_clean is True
    assert "lint failed" in result.detail


def test_workflow_keeps_patch_trial_opt_in_read_only_and_disposable():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "copilot-patch-trial:" in workflow
    assert "startsWith(github.event.workflow_run.head_branch, 'ci-agent-patch-trial/')" in workflow
    assert "needs: [triage, copilot-advisor]" in workflow
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "copilot-requests: write" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "path: candidate" in workflow
    assert workflow.count("persist-credentials: false") >= 4
    assert "python-version: '3.12'" in workflow
    assert "npm install -g @github/copilot@1.0.83" in workflow
    assert "python -m scripts.ci_copilot_patch_trial_entrypoint" in workflow
    assert "--candidate-dir candidate" in workflow
    assert "secrets." not in workflow


def test_patch_trial_has_no_persistent_git_mutation_commands():
    source = SCRIPT.read_text(encoding="utf-8")

    assert '["git", "commit"' not in source
    assert '["git", "push"' not in source
    assert "contents: write" not in source
    assert "git apply" in source
    assert '["git", "reset", "--hard", "HEAD"]' in source
    assert '["git", "clean", "-fdx"]' in source
