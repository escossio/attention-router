from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import ci_copilot_autofix_proposal_v21 as v21


WORKFLOW = Path(".github/workflows/ci-agent.yml")


def _payload(*, old_text: str, new_text: str) -> dict[str, object]:
    return {
        "schema": v21.EDIT_SCHEMA,
        "decision": "EDIT",
        "confidence": "HIGH",
        "summary": "Remove the controlled synthetic assertion.",
        "edits": [
            {
                "path": "tests/test_example.py",
                "old_text": old_text,
                "new_text": new_text,
            }
        ],
    }


def test_structured_edit_generates_git_applicable_deterministic_patch(tmp_path):
    original = (
        "def test_example():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_controlled_failure():\n"
        "    assert False, \"CONTROLLED_V2_PERSISTENT_AUTOFIX_PROOF\"\n"
    )
    old_text = (
        "def test_controlled_failure():\n"
        "    assert False, \"CONTROLLED_V2_PERSISTENT_AUTOFIX_PROOF\"\n"
    )
    new_text = "def test_controlled_failure():\n    assert True\n"

    proposal = v21.validate_structured_edit_payload(
        _payload(old_text=old_text, new_text=new_text),
        candidate_files={"tests/test_example.py": original},
        patchable_files=("tests/test_example.py",),
    )

    assert proposal.decision == "PATCH"
    assert proposal.confidence == "HIGH"
    assert proposal.target_files == ("tests/test_example.py",)
    assert "--- a/tests/test_example.py" in proposal.patch
    assert "+++ b/tests/test_example.py" in proposal.patch
    assert "CONTROLLED_V2_PERSISTENT_AUTOFIX_PROOF" in proposal.patch

    repo = tmp_path / "repo"
    target = repo / "tests" / "test_example.py"
    target.parent.mkdir(parents=True)
    target.write_text(original, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    checked = subprocess.run(
        ["git", "apply", "--check", "--whitespace=error-all", "-"],
        cwd=repo,
        input=proposal.patch,
        text=True,
        capture_output=True,
        check=False,
    )

    assert checked.returncode == 0, checked.stderr


def test_structured_edit_requires_unique_exact_old_text():
    duplicate = "assert False\nassert False\n"

    with pytest.raises(v21.StructuredEditError, match="exactly one region"):
        v21.validate_structured_edit_payload(
            _payload(old_text="assert False", new_text="assert True"),
            candidate_files={"tests/test_example.py": duplicate},
            patchable_files=("tests/test_example.py",),
        )


def test_structured_edit_rejects_non_allowlisted_file_and_low_confidence():
    payload = _payload(old_text="assert False", new_text="assert True")
    payload["edits"] = [
        {
            "path": "attention_router/core/authority.py",
            "old_text": "assert False",
            "new_text": "assert True",
        }
    ]
    with pytest.raises(v21.StructuredEditError, match="outside the V2 test allowlist"):
        v21.validate_structured_edit_payload(
            payload,
            candidate_files={"tests/test_example.py": "assert False\n"},
            patchable_files=("tests/test_example.py",),
        )

    payload = _payload(old_text="assert False", new_text="assert True")
    payload["confidence"] = "MEDIUM"
    with pytest.raises(v21.StructuredEditError, match="HIGH confidence"):
        v21.validate_structured_edit_payload(
            payload,
            candidate_files={"tests/test_example.py": "assert False\n"},
            patchable_files=("tests/test_example.py",),
        )


def test_non_edit_decision_cannot_smuggle_edits():
    payload = _payload(old_text="assert False", new_text="assert True")
    payload["decision"] = "NEEDS_HUMAN"
    payload["confidence"] = "LOW"

    with pytest.raises(v21.StructuredEditError, match="must not contain edits"):
        v21.validate_structured_edit_payload(
            payload,
            candidate_files={"tests/test_example.py": "assert False\n"},
            patchable_files=("tests/test_example.py",),
        )


def test_prompt_forbids_model_authored_git_diff():
    prompt = v21.build_edit_prompt(
        failure_packet={"category": "TEST_FAILURE"},
        candidate_files={"tests/test_example.py": "assert False\n"},
    )

    assert "Do not write a unified diff" in prompt
    assert "old_text must be copied verbatim" in prompt
    assert "attention-router-ci-copilot-structured-edit.v2.1" in prompt


def test_workflow_uses_v21_read_only_proposal_and_keeps_split_authority():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    proposal_job = workflow.split("  autofix-proposal:", 1)[1].split("  autofix-persist:", 1)[0]
    persist_job = workflow.split("  autofix-persist:", 1)[1]

    assert "python -m scripts.ci_copilot_autofix_proposal_v21" in proposal_job
    assert "python -m scripts.ci_copilot_autofix_proposal\n" not in proposal_job
    assert "contents: read" in proposal_job
    assert "contents: write" not in proposal_job
    assert "copilot-requests: write" in proposal_job
    assert "contents: write" in persist_job
    assert "copilot-requests: write" not in persist_job
    assert workflow.count("contents: write") == 1
