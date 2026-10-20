"""Safety contract: skipping a heavy suite requires COMPLETE trusted evidence."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
POLICY_ROOT = ROOT / "ops/provisioning/github-app-control-plane"
sys.path.insert(0, str(POLICY_ROOT))
from ci_impact_policy import (REPO, SUITES, classify_files, classify_compare,  # noqa: E402
                              fail_closed, get_pr_impact)


def file(name, status="modified", previous_filename=None):
    d = {"filename": name, "status": status}
    if previous_filename is not None:
        d["previous_filename"] = previous_filename
    return d


def check(names, expected):
    result = classify_files([file(p) for p in names])
    assert result.suites == frozenset(expected), result
    assert len(result.paths_sha256) == 64
    assert result.file_count == len(set(names))


def test_safely_skip_non_executable_documentation_and_status():
    check(["STATUS.md", "docs/architecture/overview.md", "docs/assets/banner.png"], ())


def test_readme_is_docker_input_not_generic_documentation():
    check(["README.md"], ("docker",))


def test_transport_source_and_tests_are_distinct():
    check(["whatsapp-transport-local/test/bridge.test.js"], ("transport",))
    check(["whatsapp-transport-local/src/bridge.js"], ("transport", "docker"))
    check(["docs/public/cognitive-architecture/app.js"], ("transport",))


def test_python_legacy_tests_do_not_require_postgres_or_docker():
    check(["tests/test_repetition.py"], ("python",))
    check(["tests/integration/test_postgres_first_contact.py"], ("python", "postgres"))


def test_schema_and_migrations_keep_postgres_and_docker():
    check(["alembic/versions/0061_tts_outbox_constraint.py"], ("python", "postgres", "docker"))
    check(["attention_router/application/services.py"], SUITES)
    check(["ops/whatsapp-container/compose.yaml"], ("python", "transport", "docker"))


def test_security_policy_ci_unknown_and_executable_docs_fail_closed():
    for name in (".github/workflows/ci.yml", "ops/provisioning/github-app-control-plane/ci_impact_policy.py",
                 "scripts/new_build_tool.py", "docs/ops/installer.sh", "pyproject.toml",
                 "attention_router/application/agents/instructions.md"):
        check([name], SUITES)


def test_mixed_changes_union_all_impacts():
    check(["STATUS.md", "whatsapp-transport-local/src/server.js", "tests/integration/test_postgres_first_contact.py"], SUITES)


def test_rename_evaluates_both_source_and_target():
    moved = classify_files([file("docs/notes.md", "renamed", "attention_router/critical.py")])
    assert moved.suites == frozenset(SUITES)
    renamed_doc = classify_files([file("docs/new.md", "renamed", "docs/old.md")])
    assert not renamed_doc.suites


def test_invalid_empty_truncated_diff_always_requires_full_suite():
    for files in ([], None, [file("docs/new.md", "renamed")], [file("../invalid.md")],
                  [file("docs/file.md", "broken")], [file("STATUS.md")] * 3000):
        outcome = classify_files(files)
        assert outcome.suites == frozenset(SUITES) and outcome.reason.startswith("FAIL_CLOSED")
    assert fail_closed("network").required("postgres")


def fake_pr_request(sha, names, *, expected=None, fail_at=None):
    result = [file(name) for name in names]
    num = expected if expected is not None else len(result)
    calls = []
    def call(path):
        calls.append(path)
        if path == fail_at:
            raise OSError("synthetic transport timeout")
        if path.endswith("/pulls/17"):
            return 200, {"head": {"sha": sha, "repo": {"full_name": REPO}},
                         "base": {"ref": "main"}, "state": "open", "changed_files": num}
        if "/files?per_page=100&page=" in path:
            page = int(path.rsplit("=", 1)[-1])
            return 200, result[(page-1)*100:page*100]
        raise ValueError("unexpected API")
    return call, calls


def test_pr_file_pagination_and_exact_head():
    sha="a"*40
    names=[f"docs/articles/{n}.md" for n in range(105)]
    api,calls=fake_pr_request(sha,names)
    result=get_pr_impact(REPO,17,sha,api)
    assert result.file_count==105 and not result.suites
    assert any("page=2" in path for path in calls)
    assert get_pr_impact(REPO,17,"b"*40,api).suites==frozenset(SUITES)


def test_pr_api_error_or_partial_file_list_runs_everything():
    sha="a"*40
    api,_=fake_pr_request(sha,["STATUS.md"],expected=3)
    assert get_pr_impact(REPO,17,sha,api).suites==frozenset(SUITES)
    api,_=fake_pr_request(sha,["STATUS.md"],fail_at=f"/repos/{REPO}/pulls/17/files?per_page=100&page=1")
    assert get_pr_impact(REPO,17,sha,api).suites==frozenset(SUITES)


def test_push_compare_and_truncation():
    before,after="a"*40,"b"*40
    payload={"status":"ahead", "total_commits":1, "files":[file("STATUS.md")]}
    assert not classify_compare(before,after,payload).suites
    assert classify_compare(before,after,{**payload,"files": [file("STATUS.md")]*300}).suites==frozenset(SUITES)
    assert classify_compare("0"*40, after, {**payload,"status":"diverged"}).suites==frozenset(SUITES)


def test_actions_classifier_invocation_with_synthetic_event():
    script=ROOT/"scripts/ci_impact_report.py"
    spec=importlib.util.spec_from_file_location("ci_impact_report_test",script)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sha="a"*40
    request,_=fake_pr_request(sha,["STATUS.md"])
    event={"number":17,"pull_request":{"head":{"sha":sha},"base":{"ref":"main"}}}
    result=module.impact_for_event("pull_request",event,REPO,"different-merge-sha",api=request)
    assert not result.suites
    result=module.impact_for_event("push",{"ref":"refs/heads/main","before":"b"*40,"after":sha},REPO,sha,
                                   api=lambda _p:(200,{"status":"ahead","total_commits":1,"files":[file("STATUS.md")]}))
    assert not result.suites
    assert module.impact_for_event("workflow_dispatch",{},REPO,sha).suites==frozenset(SUITES)


def test_gha_workflow_keeps_all_required_names_and_skips_explicitly():
    workflow=(ROOT/".github/workflows/ci.yml").read_text()
    for name in ("python-tests", "transport-tests", "docker-build", "secret-scan"):
        assert f"  {name}:" in workflow
    for suite in ("python", "transport", "docker"):
        assert f"needs.impact.outputs.{suite} != 'false'" in workflow
        assert f"needs.impact.outputs.{suite} == 'false'" in workflow
    assert "ref: ${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.sha }}" in workflow
    assert "FAIL_CLOSED_POLICY_BOOTSTRAP_OR_FAILURE" in workflow
