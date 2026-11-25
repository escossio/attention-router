import json
import runpy
import subprocess
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
DISTRIBUTED_OUTPUTS = {
    "distributed-python": {
        "title": "Distributed python shadow gate passed",
        "summary": f"Exact SHA: {SHA}\nSuite: python\nSelected worker: ci02\n| 1 | ci02 | PASS | 25 s |",
    },
    "distributed-postgres": {
        "title": "Distributed PostgreSQL gate passed",
        "summary": (f"Exact SHA: {SHA}\nTests passed: 10\nSelected tests: 12\nSelected files: 3\n"
                    f"Selection SHA-256: {'a' * 64}\n| CI03 | PASS | 55 s | 10 | 3 |"),
    },
}


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
             "status": "completed", "conclusion": "success", "output": dict(DISTRIBUTED_OUTPUTS.get(name, {}))}
            for name in sorted(gate["REQUIRED_CHECKS"])
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

    def check(operation="schema_apply", artifact="a" * 64, *, archive=None, return_record=False):
        api["issues/comments/7"]["body"] = "```json\n" + json.dumps(receipt) + "\n```"
        return verify(7, operation, tmp_path, artifact, archive=archive, return_record=return_record,
                      fetch=lambda endpoint, **_: api[endpoint], now=NOW)

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


@pytest.mark.parametrize(("case", "reason"), [
    ("unmerged", "integração do PR"),
    ("wrong_base", "integração do PR"),
    ("wrong_repository", "integração do PR"),
    ("author_review", "revisão favorável"),
    ("changes_requested", "pedido de alteração"),
    ("diverged", "linhagem"),
    ("wrong_tree", "árvore local"),
])
def test_promotion_refuses_unverified_pr_and_checkout(release, case, reason):
    check, _, api, git, _ = release
    pr = api["pulls/5"]
    reviews = api["pulls/5/reviews?per_page=100"]
    if case == "unmerged":
        pr["merged"] = False
    elif case == "wrong_base":
        pr["base"]["ref"] = "feature"
    elif case == "wrong_repository":
        pr["base"]["repo"]["full_name"] = "other/project"
    elif case == "author_review":
        reviews[0]["user"] = pr["user"]
    elif case == "changes_requested":
        reviews.append(dict(reviews[0], id=2, user={"login": "second"}, state="CHANGES_REQUESTED"))
    elif case == "diverged":
        api[f"compare/{SHA}...main?per_page=1"]["status"] = "diverged"
    else:
        git["rev-parse", "HEAD^{tree}"] = "4" * 40
    with pytest.raises(Blocked, match=reason):
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


@pytest.mark.parametrize("name", DISTRIBUTED_OUTPUTS)
def test_impact_only_success_is_not_distributed_execution(release, name):
    check, _, api, _, commands = release
    result = next(item for item in api[CHECK_RUNS] if item["name"] == name)
    result["output"] = {"title": f"NOT APPLICABLE: {name} (no test run)", "summary": "CI_IMPACT=NOT_APPLICABLE"}
    with pytest.raises(Blocked, match="recibo de execução"):
        check()
    assert not commands


@pytest.mark.parametrize(("name", "old", "new"), [
    ("distributed-python", SHA, HEAD),
    ("distributed-python", "Suite: python", "Suite: transport"),
    ("distributed-python", "Selected worker: ci02", "Selected worker: ci01"),
    ("distributed-python", "PASS", "WORKER_LOST"),
    ("distributed-postgres", SHA, HEAD),
    ("distributed-postgres", "Selected tests: 12\n", ""),
    ("distributed-postgres", "Selected files: 3", "Selected files: 3\nSelected files: 2"),
    ("distributed-postgres", "Selected files: 3", "Selected files: 4"),
    ("distributed-postgres", "Selected tests: 12", "Selected tests: 8"),
    ("distributed-postgres", "Tests passed: 10", "Tests passed: 0"),
    ("distributed-postgres", "a" * 64, "pending"),
    ("distributed-postgres", "CI03 | PASS", "CI03 | FAIL"),
])
def test_inconsistent_distributed_receipt_stops_preflight(release, name, old, new):
    check, _, api, _, commands = release
    result = next(item for item in api[CHECK_RUNS] if item["name"] == name)
    assert old in result["output"]["summary"]
    result["output"]["summary"] = result["output"]["summary"].replace(old, new)
    with pytest.raises(Blocked, match="recibo de execução"):
        check()
    assert not commands


@pytest.mark.parametrize(("reference", "accepted"), [
    (f"https://github.com/{REPO}/issues/428#issuecomment-123", True),
    (f"{REPO}#428", True),
    ("https://github.com/another/project/issues/428", False),
    ("another/project#428", False),
    ("https://example.org/issues/428", False),
    ("#4280", False),
])
def test_pr_reference_identifies_the_repository_gate(release, reference, accepted):
    check, _, api, _, _ = release
    api["pulls/5"]["body"] = f"Gate {reference}"
    if accepted:
        assert check() == (SHA, 5)
    else:
        with pytest.raises(Blocked, match="referência ao gate"):
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


def test_runtime_requires_immutable_oci_attestation(release, monkeypatch, tmp_path):
    from types import SimpleNamespace

    check, receipt, _, _, commands = release
    digest = "a" * 64
    config = f"sha256:{'b' * 64}"
    manifest = f"{REPO}@sha256:{digest}"
    reference = f"ghcr.io/{manifest}"
    archive = tmp_path / "private-context.oci.tar"
    archive.write_bytes(b"synthetic OCI fixture")
    receipt.update(gate="G5", operation="runtime_apply", artifact_sha256=digest)

    def inspect(path, source, *, with_config):
        assert path == archive and source == SHA and with_config
        return f"sha256:{digest}", config

    monkeypatch.setitem(verify.__globals__, "runpy",
                        SimpleNamespace(run_path=lambda _: {"inspect_image": inspect}))
    with pytest.raises(Blocked, match="imagem OCI"):
        check("runtime_apply", f"ghcr.io/{REPO}:latest", archive=archive)
    with pytest.raises(Blocked, match="arquivo OCI necessário"):
        check("runtime_apply", reference)
    result = check("runtime_apply", reference, archive=archive, return_record=True)
    assert result == {**receipt, "oci_image_id": config}
    assert commands[-1] == (
        "gh", "attestation", "verify", str(archive), "--repo", REPO,
        "--signer-workflow", f"{REPO}/.github/workflows/private-context-oci.yml", "--source-ref", "refs/heads/main",
        "--source-digest", SHA, "--deny-self-hosted-runners",
    )

    def wrong_manifest(*args, **kwargs):
        return f"sha256:{'c' * 64}", config

    monkeypatch.setitem(verify.__globals__, "runpy",
                        SimpleNamespace(run_path=lambda _: {"inspect_image": wrong_manifest}))
    with pytest.raises(Blocked, match="manifesto OCI"):
        check("runtime_apply", reference, archive=archive)


def test_failed_oci_attestation_blocks_promotion(release, monkeypatch, tmp_path):
    check, receipt, _, _, _ = release
    receipt.update(gate="G6", operation="canary_enable")
    archive = tmp_path / "private-context.oci.tar"
    archive.write_bytes(b"synthetic OCI fixture")
    original = verify.__globals__["run"]

    def command(*args):
        if args[0] == "gh":
            raise subprocess.CalledProcessError(1, args)
        return original(*args)

    monkeypatch.setitem(verify.__globals__, "run", command)
    with pytest.raises(Blocked, match="atestado OCI"):
        check("canary_enable", f"ghcr.io/{REPO}@sha256:{'a' * 64}", archive=archive)


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


@pytest.mark.parametrize(("kind", "window", "value"), [
    ("schema", "ANDY_SCHEMA_RELEASE_EXPLICIT", "SCHEMA_ONLY_NO_TENANT"),
    ("roles", "ANDY_ROLES_RELEASE_EXPLICIT", "ROLES_ONLY_NO_TENANT"),
])
@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_required_execution_refusal_precedes_db_and_catalog(release, monkeypatch, tmp_path, kind, window, value, action):
    from attention_router.config import settings

    check, receipt, api, _, _ = release
    operation = f"{kind}_{action}"
    receipt.update(operation=operation, gate=gate["OPERATIONS"][operation])
    next(item for item in api[CHECK_RUNS] if item["name"] == "distributed-postgres")["output"] = {}
    runner = runpy.run_path(str(MODULE.with_name(f"release_{kind}.py")))["run"]
    run_path = runpy.run_path

    def load(path):
        if Path(path) == MODULE:
            return {"verify": lambda comment, op, *args, **kwargs: check(op)}
        return run_path(path)

    monkeypatch.setattr(runpy, "run_path", load)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setenv(window, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"approval_ref": receipt["approval_ref"]}))
    approval.chmod(0o600)
    unread = tmp_path / "absent.json"
    options = {"scope_path": unread} if kind == "roles" else {}
    with pytest.raises(Blocked, match="recibo de execução"):
        runner(action, root=MODULE.parents[2], catalog_path=unread, acl_path=unread,
               approval_path=approval, github_comment_id=7, **options)


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


# V2 fixtures are synthetic and deliberately do not create GitHub approvals.
# Existing V1 tests above remain unchanged.
@pytest.fixture()
def single_owner_release(release):
    from copy import deepcopy
    from hashlib import sha256

    check, receipt, api, git, commands = release
    uid = 101
    owner = {"login": "escossio", "id": uid, "type": "User"}
    all_permissions = dict.fromkeys(("pull", "triage", "push", "maintain", "admin"), True)
    api["@repository"] = {
        "full_name": REPO, "default_branch": "main", "owner": dict(owner),
        "permissions": dict(all_permissions),
    }
    api["@authenticated-user"] = dict(owner)
    api["collaborators?affiliation=all&per_page=100"] = [
        {**owner, "role_name": "admin", "permissions": dict(all_permissions)}
    ]
    pr = api["pulls/5"]
    pr.update(user=dict(owner), author_association="OWNER", merged_at="2026-01-01T11:45:00Z")
    pr["head"]["repo"] = {"full_name": REPO}
    api["pulls/5/reviews?per_page=100"] = []
    api[f"git/commits/{SHA}"]["parents"] = [{"sha": "4" * 40}, {"sha": HEAD}]
    official = {name: 4982054 if name.startswith("distributed-") else 15368
                for name in gate["REQUIRED_CHECKS"]}
    protection = api["branches/main/protection/required_status_checks"]
    protection["strict"] = True
    for c in protection["checks"]:
        c["app_id"] = official[c["context"]]
    api["branches/main/protection"] = {
        "enforce_admins": {"enabled": True}, "required_pull_request_reviews": None,
        "required_status_checks": protection,
    }
    api[BRANCH_RULES] = [
        {"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [
                {"context": name, "integration_id": app} for name, app in official.items()
            ],
        }},
        {"type": "pull_request", "parameters": {
            "required_approving_review_count": 0, "require_code_owner_review": False,
            "require_last_push_approval": False, "required_review_thread_resolution": True,
            "required_reviewers": [],
        }},
    ]
    integrated = api[CHECK_RUNS]
    for n, c in enumerate(integrated, 100):
        c.update(id=n, app={"id": official[c["name"]]}, completed_at="2026-01-01T11:50:00Z")
        if c["name"] == "distributed-postgres":
            c["output"]["summary"] = c["output"]["summary"].replace(
                "Tests passed: 10", "Tests passed: 12").replace("| 10 | 3 |", "| 12 | 3 |")
    heads = deepcopy(integrated)
    for c in heads:
        c.update(id=c["id"] + 100, head_sha=HEAD, completed_at="2026-01-01T11:42:00Z")
        if c["name"] in DISTRIBUTED_OUTPUTS:
            c["output"]["summary"] = c["output"]["summary"].replace(SHA, HEAD)
    api[f"commits/{HEAD}/check-runs?filter=all&per_page=100"] = heads

    def identities(rows):
        # Independent construction of the accepted evidence; never use the verifier to
        # derive the expected value for the positive test.
        return {r["name"]: {
            "app_id": r["app"]["id"], "check_run_id": r["id"], "completed_at": r["completed_at"],
            "output_sha256": sha256(json.dumps(r["output"], sort_keys=True,
                                              ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        } for r in rows}

    accepted = {
        "version": 2, "record_type": "technical_acceptance", "mode": "single_owner",
        "decision": "accepted", "assurance": "automated_evidence_not_independent_human_review",
        "gate": receipt["gate"], "operation": receipt["operation"], "pr": receipt["pr"],
        "sha": SHA, "head": HEAD, "tree": TREE, "artifact_sha256": receipt["artifact_sha256"],
        "expires_at": "2026-01-01T12:30:00Z",
        "checks": {"head": identities(heads), "integrated": identities(integrated)},
    }
    op_comment = api["issues/comments/7"]
    op_comment.update(id=7, user=dict(owner), created_at=op_comment["updated_at"])
    technical = api["issues/comments/6"] = {
        "id": 6, "user": dict(owner), "issue_url": op_comment["issue_url"],
        "created_at": "2026-01-01T11:52:00Z", "updated_at": "2026-01-01T11:52:00Z",
    }
    receipt.update(version=2, record_type="operation_authorization", mode="single_owner",
                   technical_acceptance_id=6)

    def reseal():
        technical["body"] = "\x60\x60\x60json\n" + json.dumps(accepted) + "\n\x60\x60\x60"
        receipt["technical_acceptance_sha256"] = sha256(technical["body"].encode()).hexdigest()

    reseal()
    return check, receipt, api, git, commands, accepted, reseal


@pytest.mark.parametrize("operation", ["schema_apply", "schema_rollback", "roles_apply", "roles_rollback"])
def test_single_owner_v2_explicit_separate_acceptance(single_owner_release, operation):
    check, record, _, _, _, accepted, reseal = single_owner_release
    record.update(operation=operation, gate=gate["OPERATIONS"][operation])
    accepted.update(operation=operation, gate=record["gate"])
    reseal()
    assert check(operation) == (SHA, 5)
    assert check(operation, return_record=True) == record


@pytest.mark.parametrize("operation", ["runtime_apply", "runtime_rollback", "canary_enable", "canary_disable"])
def test_single_owner_v2_preserves_attestation(single_owner_release, monkeypatch, tmp_path, operation):
    from types import SimpleNamespace

    check, record, _, _, commands, accepted, reseal = single_owner_release
    record.update(operation=operation, gate=gate["OPERATIONS"][operation])
    accepted.update(operation=operation, gate=record["gate"])
    reseal()
    archive = tmp_path / "synthetic.oci.tar"
    archive.write_bytes(b"synthetic fixture")
    reference = f"ghcr.io/{REPO}@sha256:{'a' * 64}"
    config = f"sha256:{'b' * 64}"
    monkeypatch.setitem(verify.__globals__, "runpy", SimpleNamespace(
        run_path=lambda _: {"inspect_image": lambda *a, **k: ("sha256:" + "a" * 64, config)}
    ))
    with pytest.raises(Blocked, match="arquivo OCI necessário"):
        check(operation, reference)
    result = check(operation, reference, archive=archive, return_record=True)
    assert result == {**record, "oci_image_id": config}
    attestations = [cmd for cmd in commands if cmd[:3] == ("gh", "attestation", "verify")]
    assert len(attestations) == 1 and attestations[0][-3:] == (
        "--source-digest", SHA, "--deny-self-hosted-runners",
    )
    assert commands[-1][0] == "git"  # Fresh checkout verification after attestation.


def test_no_automatic_single_owner_fallback(release):
    check, _, api, _, _ = release
    api["pulls/5/reviews?per_page=100"] = []
    with pytest.raises(Blocked, match="revisão favorável"):
        check()


@pytest.mark.parametrize("case,reason", [
    ("actor", "identidade ou autoridade"),
    ("owner_type", "identidade ou autoridade"),
    ("admin", "identidade ou autoridade"),
    ("author", "PR não pertence"),
    ("foreign_head", "PR não pertence"),
    ("write_collaborator", "outra autoridade"),
    ("duplicate_collaborator", "inventário de colaboradores"),
    ("incomplete_permissions", "inventário de colaboradores"),
    ("empty_inventory", "inventário de colaboradores"),
    ("missing_owner", "outra autoridade"),
    ("technical_author", "autor divergente"),
    ("technical_uid", "autor do registro"),
    ("request_changes", "pedido de alteração"),
])
def test_single_owner_v2_authority_refusals(single_owner_release, case, reason):
    from copy import deepcopy
    check, _, api, _, _, _, _ = single_owner_release
    collaborators = api["collaborators?affiliation=all&per_page=100"]
    pr = api["pulls/5"]
    if case == "actor":
        api["@authenticated-user"]["id"] = 102
    elif case == "owner_type":
        api["@repository"]["owner"]["type"] = "Organization"
    elif case == "admin":
        api["@repository"]["permissions"]["admin"] = False
    elif case == "author":
        pr["user"] = {"login": "other", "id": 102}
    elif case == "foreign_head":
        pr["head"]["repo"]["full_name"] = "other/project"
    elif case == "write_collaborator":
        collaborators.append(dict(deepcopy(collaborators[0]), id=102, login="another"))
    elif case == "duplicate_collaborator":
        collaborators.append(deepcopy(collaborators[0]))
    elif case == "incomplete_permissions":
        del collaborators[0]["permissions"]["push"]
    elif case == "empty_inventory":
        collaborators.clear()
    elif case == "missing_owner":
        collaborators[0].update(id=102, login="another")
    elif case == "technical_author":
        api["issues/comments/6"]["user"]["login"] = "other"
    elif case == "technical_uid":
        api["issues/comments/6"]["user"]["id"] = 102
    else:
        api["pulls/5/reviews?per_page=100"].append({
            "id": 9, "submitted_at": "2026-01-01T11:51:00Z", "state": "CHANGES_REQUESTED",
            "commit_id": HEAD, "author_association": "COLLABORATOR", "user": {"login": "another"},
        })
    with pytest.raises(Blocked, match=reason):
        check()


def test_single_owner_v2_allows_read_only_collaborator(single_owner_release):
    check, _, api, _, _, _, _ = single_owner_release
    api["collaborators?affiliation=all&per_page=100"].append({
        "id": 102, "login": "reader", "type": "User", "role_name": "read",
        "permissions": {"pull": True, "triage": False, "push": False, "maintain": False, "admin": False},
    })
    assert check() == (SHA, 5)


@pytest.mark.parametrize("case,reason", [
    ("mode", "contrato operacional"),
    ("v1_markers", "V1 não pode"),
    ("same_comment", "registro distinto"),
    ("bad_hash", "digest do aceite"),
    ("edited_technical", "editado ou revogado"),
    ("edited_operation", "editado ou revogado"),
    ("revoked", "revogado ou inválido"),
    ("wrong_operation", "outra operação"),
    ("wrong_sha", "outra operação"),
    ("wrong_head", "outra operação"),
    ("wrong_artifact", "outra operação"),
    ("wrong_tree", "árvore integrada"),
    ("wrong_parent", "não preserva"),
    ("technical_after_operation", "deve preceder"),
    ("technical_before_merge", "anterior à integração"),
    ("operation_too_long", "fora da janela"),
    ("technical_too_long", "fora da janela"),
    ("expiry", "fora da janela"),
    ("non_utc", "deve usar UTC"),
    ("no_assurance", "revogado ou inválido"),
])
def test_single_owner_v2_record_refusals(single_owner_release, case, reason):
    check, record, api, _, _, accepted, reseal = single_owner_release
    tech, op = api["issues/comments/6"], api["issues/comments/7"]
    changed_technical = False
    if case == "mode":
        record["mode"] = "automatic"
    elif case == "v1_markers":
        record["version"] = 1
    elif case == "same_comment":
        record["technical_acceptance_id"] = 7
    elif case == "bad_hash":
        record["technical_acceptance_sha256"] = "f" * 64
    elif case == "edited_technical":
        tech["updated_at"] = "2026-01-01T11:54:00Z"
    elif case == "edited_operation":
        op["updated_at"] = "2026-01-01T11:56:00Z"
    elif case == "revoked":
        accepted["decision"] = "revoked"
        changed_technical = True
    elif case == "wrong_operation":
        accepted["operation"] = "schema_rollback"
        changed_technical = True
    elif case == "wrong_sha":
        accepted["sha"] = HEAD
        changed_technical = True
    elif case == "wrong_head":
        accepted["head"] = SHA
        changed_technical = True
    elif case == "wrong_artifact":
        accepted["artifact_sha256"] = "b" * 64
        changed_technical = True
    elif case == "wrong_tree":
        accepted["tree"] = "f" * 40
        changed_technical = True
    elif case == "wrong_parent":
        api[f"git/commits/{SHA}"]["parents"][1]["sha"] = "f" * 40
    elif case == "technical_after_operation":
        tech["created_at"] = tech["updated_at"] = "2026-01-01T11:56:00Z"
    elif case == "technical_before_merge":
        tech["created_at"] = tech["updated_at"] = "2026-01-01T11:40:00Z"
    elif case == "operation_too_long":
        record["expires_at"] = "2026-01-01T12:26:00Z"
    elif case == "technical_too_long":
        accepted["expires_at"] = "2026-01-03T12:00:00Z"
        changed_technical = True
    elif case == "expiry":
        accepted["expires_at"] = "2026-01-01T11:59:00Z"
        changed_technical = True
    elif case == "non_utc":
        record["expires_at"] = "2026-01-01T13:15:00+01:00"
    else:
        accepted["assurance"] = "independent_human_review"
        changed_technical = True
    if changed_technical:
        reseal()
    with pytest.raises(Blocked, match=reason):
        check()


@pytest.mark.parametrize("case,reason", [
    ("other_app", "política estrita ou Apps oficiais"),
    ("classic_admin", "não se aplica ao administrador"),
    ("classic_review", "revisão clássica obrigatória"),
    ("strict_rule", "ruleset estrito"),
    ("mandatory_review", "regras de revisão"),
    ("head_missing", "check ausente"),
    ("latest_replaced", "identidade ou recibo"),
    ("receipt_changed", "identidade ou recibo"),
    ("receipt_missing", "recibo de execução"),
    ("partial_postgres", "não cobre toda a seleção"),
    ("late_check", "concluído após"),
    ("new_failure", "sem sucesso"),
    ("dirty_checkout", "checkout com alterações"),
])
def test_single_owner_v2_certification_refusals(single_owner_release, case, reason):
    from copy import deepcopy
    check, _, api, git, _, _, _ = single_owner_release
    checks = api[CHECK_RUNS]
    pg = next(c for c in checks if c["name"] == "distributed-postgres")
    if case == "other_app":
        for c in api["branches/main/protection/required_status_checks"]["checks"]:
            if c["context"] == "distributed-postgres":
                c["app_id"] = 42
        for c in api[BRANCH_RULES][0]["parameters"]["required_status_checks"]:
            if c["context"] == "distributed-postgres":
                c["integration_id"] = 42
    elif case == "classic_admin":
        api["branches/main/protection"]["enforce_admins"]["enabled"] = False
    elif case == "classic_review":
        api["branches/main/protection"]["required_pull_request_reviews"] = {
            "required_approving_review_count": 1}
    elif case == "strict_rule":
        api[BRANCH_RULES][0]["parameters"]["strict_required_status_checks_policy"] = False
    elif case == "mandatory_review":
        api[BRANCH_RULES][1]["parameters"]["required_approving_review_count"] = 1
    elif case == "head_missing":
        api[f"commits/{HEAD}/check-runs?filter=all&per_page=100"].pop()
    elif case == "latest_replaced":
        checks.append(dict(deepcopy(pg), id=999))
    elif case == "receipt_changed":
        pg["output"]["summary"] += "\nAdditional changed content"
    elif case == "receipt_missing":
        pg["output"] = {}
    elif case == "partial_postgres":
        pg["output"]["summary"] = pg["output"]["summary"].replace(
            "Tests passed: 12", "Tests passed: 10").replace("| 12 | 3 |", "| 10 | 3 |")
    elif case == "late_check":
        pg["completed_at"] = "2026-01-01T11:53:00Z"
    elif case == "new_failure":
        checks.append(dict(deepcopy(pg), id=999, conclusion="failure"))
    else:
        git[next(k for k in git if k[0] == "status")] = "?? extra.py"
    with pytest.raises(Blocked, match=reason):
        check()


@pytest.mark.parametrize("action", ["schema_apply", "schema_rollback", "roles_apply", "roles_rollback"])
def test_single_owner_v2_rejection_before_database(single_owner_release, monkeypatch, tmp_path, action):
    from attention_router.config import settings
    check, record, api, _, _, accepted, reseal = single_owner_release
    kind, verb = action.split("_")
    record.update(operation=action, gate=gate["OPERATIONS"][action])
    accepted.update(operation=action, gate=record["gate"])
    reseal()
    api["collaborators?affiliation=all&per_page=100"].clear()
    runner = runpy.run_path(str(MODULE.with_name(f"release_{kind}.py")))["run"]
    run_path = runpy.run_path
    monkeypatch.setattr(runpy, "run_path", lambda path: (
        {"verify": lambda comment, op, *a, **kw: check(op)} if Path(path) == MODULE else run_path(path)
    ))
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setenv("ANDY_SCHEMA_RELEASE_EXPLICIT", "SCHEMA_ONLY_NO_TENANT")
    monkeypatch.setenv("ANDY_ROLES_RELEASE_EXPLICIT", "ROLES_ONLY_NO_TENANT")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"approval_ref": record["approval_ref"]}))
    approval.chmod(0o600)
    missing = tmp_path / "not-read.json"
    with pytest.raises(Blocked, match="inventário de colaboradores"):
        runner(verb, root=MODULE.parents[2], catalog_path=missing, acl_path=missing,
               approval_path=approval, github_comment_id=7,
               **({"scope_path": missing} if kind == "roles" else {}))


@pytest.mark.parametrize("operation", ["runtime_apply", "runtime_rollback", "canary_enable", "canary_disable"])
def test_single_owner_v2_rejection_before_compose(single_owner_release, monkeypatch, tmp_path, operation):
    check, record, api, _, _, accepted, reseal = single_owner_release
    record.update(operation=operation, gate=gate["OPERATIONS"][operation])
    accepted.update(operation=operation, gate=record["gate"])
    reseal()
    api["collaborators?affiliation=all&per_page=100"].clear()
    runner = runpy.run_path(str(MODULE.with_name("private_runtime_release.py")))["run"]
    seen = []
    def command(args, **kwargs):
        seen.append(args)
        assert str(args[1]) == str(MODULE)  # No Docker inspect or Compose is allowed yet.
        check(operation)
    monkeypatch.setattr(subprocess, "check_output", command)
    archive = tmp_path / "synthetic.oci.tar"
    archive.write_bytes(b"synthetic")
    with pytest.raises(Blocked, match="inventário de colaboradores"):
        runner(operation, 7, f"ghcr.io/{REPO}@sha256:{'a' * 64}",
               root=MODULE.parents[2], archive=archive)
    assert len(seen) == 1


@pytest.mark.parametrize("case", ["technical_deleted", "inventory_denied", "operational_deleted"])
def test_single_owner_v2_missing_api_evidence_fails_closed(single_owner_release, case):
    check, _, api, _, commands, _, _ = single_owner_release
    if case == "technical_deleted":
        del api["issues/comments/6"]
    elif case == "inventory_denied":
        del api["collaborators?affiliation=all&per_page=100"]
    else:
        # Use the real verifier directly rather than fixture.check(), which writes body.
        del api["issues/comments/7"]
        with pytest.raises(KeyError):
            verify(7, "schema_apply", Path("."), "a" * 64,
                   fetch=lambda endpoint, **_: api[endpoint], now=NOW)
        assert not commands
        return
    with pytest.raises(KeyError):
        check()
    assert not commands


@pytest.mark.parametrize("case,reason", [
    ("technical", "registro V2 alterado"),
    ("operation", "registro V2 alterado"),
    ("collaborator", "outra autoridade"),
    ("checks", "checks mudaram"),
    ("reviews", "revisões mudaram"),
    ("pr", "PR mudou"),
])
def test_single_owner_v2_material_changes_during_preflight(single_owner_release, tmp_path, case, reason):
    from copy import deepcopy
    _, record, api, _, _, _, _ = single_owner_release
    api["issues/comments/7"]["body"] = "\x60\x60\x60json\n" + json.dumps(record) + "\n\x60\x60\x60"
    counts = {}

    def fetch(endpoint, **_):
        counts[endpoint] = counts.get(endpoint, 0) + 1
        result = deepcopy(api[endpoint])
        if counts[endpoint] < 2:
            return result
        if case == "technical" and endpoint == "issues/comments/6":
            result["updated_at"] = "2026-01-01T11:58:00Z"
        if case == "operation" and endpoint == "issues/comments/7":
            result["updated_at"] = "2026-01-01T11:59:00Z"
        if case == "collaborator" and endpoint == "collaborators?affiliation=all&per_page=100":
            result.append(dict(deepcopy(result[0]), id=102, login="new-admin"))
        if case == "checks" and endpoint == CHECK_RUNS:
            result.append(dict(deepcopy(result[0]), id=1000))
        if case == "reviews" and endpoint == "pulls/5/reviews?per_page=100":
            result.append({"state": "CHANGES_REQUESTED"})
        if case == "pr" and endpoint == "pulls/5":
            result["head"]["sha"] = "f" * 40
        return result

    with pytest.raises(Blocked, match=reason):
        verify(7, "schema_apply", tmp_path, "a" * 64, fetch=fetch, now=NOW)


def test_single_owner_v2_rejects_duplicate_json_fields(single_owner_release, tmp_path):
    _, record, api, _, _, _, _ = single_owner_release
    api["issues/comments/7"]["body"] = "\x60\x60\x60json\n" + json.dumps(record)[:-1] + ', "version": 2}\n\x60\x60\x60'
    with pytest.raises(Blocked, match="campo duplicado"):
        verify(7, "schema_apply", tmp_path, "a" * 64,
               fetch=lambda endpoint, **_: api[endpoint], now=NOW)
