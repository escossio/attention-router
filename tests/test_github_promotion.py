import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "ops/context-canary/verify_github_promotion.py"
gate = runpy.run_path(str(MODULE))
verify = gate["verify"]
Blocked = gate["PromotionBlocked"]
SHA = "1" * 40
HEAD = "2" * 40
TREE = "3" * 40
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
REPO = "escossio/attention-router"
BRANCH_RULES = "rules/branches/main?per_page=100"
CHECK_RUNS = f"commits/{SHA}/check-runs?filter=all&per_page=100"


@pytest.fixture()
def release(monkeypatch, tmp_path):
    receipt = {
        "version": 1, "gate": "G3", "operation": "schema_apply", "pr": 5,
        "artifact_sha256": "a" * 64,
        "sha": SHA, "approval_ref": "synthetic-approval", "expires_at": "2026-01-01T12:15:00Z",
    }
    api = {
        "issues/comments/7": {
            "user": {"login": "escossio"},
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/428",
            "updated_at": "2026-01-01T11:55:00Z",
        },
        "pulls/5": {
            "merged": True, "merge_commit_sha": SHA, "body": "Gate #428",
            "base": {"repo": {"full_name": REPO}, "ref": "main"},
            "user": {"login": "author"}, "head": {"sha": HEAD},
        },
        "pulls/5/reviews?per_page=100": [
            {"id": 1, "submitted_at": "2026-01-01T11:45:00Z", "state": "APPROVED",
             "commit_id": HEAD, "author_association": "MEMBER", "user": {"login": "reviewer"}},
        ],
        f"compare/{SHA}...main?per_page=1": {"merge_base_commit": {"sha": SHA}, "status": "ahead"},
        f"git/commits/{SHA}": {"sha": SHA, "tree": {"sha": TREE}},
        "branches/main/protection/required_status_checks": {"checks": [
            {"context": name, "app_id": 15368} for name in sorted(gate["REQUIRED_CHECKS"] - {"distributed-python"})
        ]},
        BRANCH_RULES: [{"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": name, "integration_id": 15368} for name in ("distributed-postgres", "distributed-python")
        ]}}],
        CHECK_RUNS: [
            {"id": 1, "name": name, "app": {"id": 15368}, "head_sha": SHA,
             "status": "completed", "conclusion": "success"} for name in sorted(gate["REQUIRED_CHECKS"])
        ],
    }
    responses = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("rev-parse", "HEAD"): SHA,
        ("rev-parse", "HEAD^{tree}"): TREE,
        ("ls-files", "-v"): "H tracked.py",
        ("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
         "--ignore-submodules=none"): "",
    }
    commands = []

    def command(*args):
        commands.append(args)
        if args[0] == "git":
            assert args[1:3] == ("-C", str(tmp_path))
            return responses[args[3:]]
        assert args[:3] == ("gh", "attestation", "verify")
        return ""

    monkeypatch.setitem(verify.__globals__, "run", command)

    def check(operation="schema_apply", artifact="a" * 64):
        api["issues/comments/7"]["body"] = "```json\n" + json.dumps(receipt) + "\n```"
        return verify(7, operation, tmp_path, artifact, fetch=lambda endpoint, **_: api[endpoint], now=NOW)

    return check, receipt, api, responses, commands


def test_approved_schema_requires_exact_git_checkout(release):
    check, _, _, git, _ = release
    assert check() == (SHA, 5)
    git["rev-parse", "HEAD"] = HEAD
    with pytest.raises(Blocked, match="SHA local"):
        check()


@pytest.mark.parametrize("status", [" M tracked.py", "?? overlay.py", "!! ignored.py"])
def test_local_overlays_block_release(release, status):
    check, _, _, git, _ = release
    git[next(args for args in git if args[0] == "status")] = status
    with pytest.raises(Blocked, match="checkout com alterações"):
        check()


def test_assume_unchanged_entries_block_release(release):
    check, _, _, git, _ = release
    git["ls-files", "-v"] = "h tracked.py"
    with pytest.raises(Blocked, match="entradas ocultas"):
        check()


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("expires_at", "2026-01-01T11:59:00Z", "fora da janela"),
    ("operation", "schema_rollback", "operação divergente"),
    ("gate", "G6", "gate ou operação"),
])
def test_approval_is_operation_bound_and_current(release, field, value, reason):
    check, receipt, _, _, _ = release
    receipt[field] = value
    with pytest.raises(Blocked, match=reason):
        check()


def test_author_and_issue_are_checked(release):
    check, _, api, _, _ = release
    comment = api["issues/comments/7"]
    comment["user"]["login"] = "other"
    with pytest.raises(Blocked, match="autor divergente"):
        check()
    comment["user"]["login"] = "escossio"
    comment["issue_url"] = comment["issue_url"].replace("428", "425")
    with pytest.raises(Blocked, match="fora da issue"):
        check()


def test_pr_result_and_review_must_match(release):
    check, _, api, _, _ = release
    pr = api["pulls/5"]
    pr["merge_commit_sha"] = HEAD
    with pytest.raises(Blocked, match="integração do PR"):
        check()
    pr["merge_commit_sha"] = SHA
    api["pulls/5/reviews?per_page=100"][0]["commit_id"] = SHA
    with pytest.raises(Blocked, match="head final"):
        check()


def test_check_cannot_be_reused_from_another_sha_or_app(release):
    check, _, api, _, _ = release
    checks = api[f"commits/{SHA}/check-runs?filter=all&per_page=100"]
    expected = checks.pop()
    checks.extend([
        dict(expected, head_sha=HEAD),
        dict(expected, app={"id": 42}),
    ])
    with pytest.raises(Blocked, match=expected["name"]):
        check()


def test_new_failed_check_supersedes_success(release):
    check, _, api, _, _ = release
    checks = api[f"commits/{SHA}/check-runs?filter=all&per_page=100"]
    checks.append(dict(checks[0], id=2, conclusion="failure"))
    with pytest.raises(Blocked, match="sem sucesso"):
        check()


def test_new_ruleset_check_must_succeed_at_exact_sha(release):
    check, _, api, _, _ = release
    api[BRANCH_RULES][0]["parameters"]["required_status_checks"].append(
        {"context": "new-required-check", "integration_id": 42},
    )
    with pytest.raises(Blocked, match="new-required-check"):
        check()
    api[CHECK_RUNS].append({
        "id": 5, "name": "new-required-check", "app": {"id": 42}, "head_sha": SHA,
        "status": "completed", "conclusion": "success",
    })
    assert check() == (SHA, 5)


def test_missing_ruleset_python_requirement_blocks_release(release):
    check, _, api, _, _ = release
    api[BRANCH_RULES].clear()
    with pytest.raises(Blocked, match="política de checks obrigatórios incompleta"):
        check()


@pytest.mark.parametrize(("app_id", "reason"), [
    (None, "App obrigatório sem vínculo"),
    (42, "Apps obrigatórios divergentes"),
])
def test_unbound_or_conflicting_ruleset_apps_block_release(release, app_id, reason):
    check, _, api, _, _ = release
    api[BRANCH_RULES][0]["parameters"]["required_status_checks"][0]["integration_id"] = app_id
    with pytest.raises(Blocked, match=reason):
        check()


def test_runtime_requires_immutable_oci_attestation(release):
    check, receipt, _, _, commands = release
    digest = "a" * 64
    receipt.update(gate="G5", operation="runtime_apply", artifact_sha256=digest)
    with pytest.raises(Blocked, match="imagem OCI"):
        check("runtime_apply", f"ghcr.io/{REPO}:latest")
    assert check("runtime_apply", f"ghcr.io/{REPO}@sha256:{digest}") == (SHA, 5)
    assert commands[-1] == (
        "gh", "attestation", "verify", f"oci://ghcr.io/{REPO}@sha256:{digest}", "--repo", REPO,
        "--signer-workflow", f"{REPO}/.github/workflows/publish-container.yml",
        "--source-digest", SHA, "--deny-self-hosted-runners",
    )


@pytest.mark.parametrize(("runner", "window", "value", "options"), [
    ("release_schema", "ANDY_SCHEMA_RELEASE_EXPLICIT", "SCHEMA_ONLY_NO_TENANT", {}),
    ("release_roles", "ANDY_ROLES_RELEASE_EXPLICIT", "ROLES_ONLY_NO_TENANT",
     {"scope_path": Path("not-read.json")}),
])
@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_release_entrypoint_requires_github_before_database(monkeypatch, runner, window, value, options, action):
    from attention_router.config import settings

    release = runpy.run_path(str(MODULE.with_name(f"{runner}.py")))
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setenv(window, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    root = MODULE.parents[2]
    with pytest.raises(RuntimeError, match="RELEASE_GITHUB_APPROVAL_REQUIRED"):
        release["run"](
            action, root=root, catalog_path=root / "not-read.json",
            acl_path=root / "not-read.json", approval_path=None, **options,
        )


def test_release_cannot_use_private_approval_for_another_record(release):
    _, receipt, api, _, _ = release
    api["issues/comments/7"]["body"] = "```json\n" + json.dumps(receipt) + "\n```"
    with pytest.raises(Blocked, match="autorização privada diverge"):
        verify(
            7, "schema_apply", Path("."), None, approval_ref="another-approval",
            fetch=lambda endpoint, **_: api[endpoint], now=NOW,
        )


@pytest.mark.parametrize("digest", [None, "b" * 64])
def test_schema_runner_requires_approved_source_digest(release, digest):
    check, _, _, _, _ = release
    with pytest.raises(Blocked, match="digest do runner"):
        check(artifact=digest)
