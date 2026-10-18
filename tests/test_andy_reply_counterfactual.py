"""A selection counterfactual is evidence for the prefix, never E2E authority."""

import json
from pathlib import Path

import pytest

from scripts.forensics.andy_reply_counterfactual import compare_sources, revision_report


ROOT = Path(__file__).resolve().parents[1]


def test_current_source_counterfactuals_preserve_unknown_conversation_and_isolation():
    report = revision_report(ROOT)
    assert report["passed"], report["cases"]
    cases = {case["case"]: case for case in report["cases"]}
    assert len(cases) == 14
    assert cases["unknown_with_explicit_default"]["agent_path_enabled"]
    assert not cases["unknown_without_default"]["agent_path_enabled"]
    assert cases["known_binding_without_default"]["agent_path_enabled"]
    assert not cases["default_of_other_tenant"]["blueprint_configured"]
    assert not cases["binding_of_other_tenant"]["blueprint_configured"]
    assert not cases["version_belongs_to_other_blueprint"]["blueprint_configured"]
    assert not report["owner_authority_proven"]
    assert not report["end_to_end_recovery_proven"]
    assert len(report["function_ast_sha256"]) == 3
    assert "5500000000001" not in json.dumps(report)


def test_counterfactual_detects_removed_configuration_dependency():
    pipeline = (ROOT / "attention_router/application/decision_pipeline.py").read_text()
    direct = (ROOT / "attention_router/application/direct_conversation.py").read_text()
    changed = pipeline.replace("return blueprint_configured and direct_conversation_eligibility(event).eligible",
                               "return direct_conversation_eligibility(event).eligible")
    assert changed != pipeline
    report = compare_sources(changed, direct)
    assert not report["passed"]
    assert any(case["case"] == "unknown_without_default" and not case["passed"] for case in report["cases"])


def test_counterfactual_rejects_incomplete_source():
    with pytest.raises(ValueError, match="definitions are missing"):
        compare_sources("", "")


def test_revision_reader_requires_explicit_commit_without_running_git():
    with pytest.raises(ValueError, match="hexadecimal Git commit"):
        revision_report(ROOT, "--help")
