"""Authority and absence-of-write contracts for merged-main CI."""
from copy import deepcopy
import importlib
from pathlib import Path
import sys

import pytest


APP = Path(__file__).resolve().parents[1] / "ops/provisioning/github-app-control-plane"
sys.path.insert(0, str(APP))
evidence = importlib.import_module("merged_main_evidence")
REPO = evidence.REPO
SHA, HEAD, BASE, TREE, NEXT = (letter * 40 for letter in "abcde")


class GitHub:
    def __init__(self, number=445):
        self.number = number
        self.base = "/repos/" + REPO + "/"
        self.pr = dict(number=number, state="closed", merged=True, merge_commit_sha=SHA,
                       base={"ref": "main", "repo": {"full_name": REPO}},
                       head={"sha": HEAD, "repo": {"full_name": REPO}})
        self.graph = {"nameWithOwner": REPO, "pullRequest": {
            "number": number, "state": "MERGED", "merged": True, "baseRefName": "main",
            "headRefOid": HEAD, "mergeCommit": {"oid": SHA}}}
        self.timeline = [{"event": "merged", "commit_id": SHA}]
        self.parents = [BASE, HEAD]
        self.tree = TREE
        self.main = SHA
        self.refs = []
        self.relation = None
        self.rules = [
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": name, "integration_id": evidence.APP_ID}
                for name in ("distributed-python", "distributed-postgres")
            ]}},
        ]
        self.checks = [
            {"id": i, "status": "completed", "conclusion": "success", "head_sha": HEAD,
             "app": {"id": evidence.APP_ID}, "name": name}
            for i, name in enumerate(("distributed-python", "distributed-postgres"), 1)
        ]
        self.app_id = evidence.APP_ID
        self.perms = {name: "read" for name in ("contents", "pull_requests", "issues", "metadata")}
        self.perms["checks"] = "write"
        self.failures = {}
        self.calls = []
        self.pr_sequence = []

    def transport(self, path, token, *, method="GET", body=None):
        self.calls.append((path, method))
        if path in self.failures:
            value = self.failures[path]
            if isinstance(value, BaseException):
                raise value
            return value
        if path == "/app":
            assert token == "synthetic-jwt" and method == "GET"
            return 200, {"id": self.app_id, "slug": evidence.APP_SLUG}
        if path == self.base + "installation":
            return 200, {"id": 123, "suspended_at": None}
        if path == "/app/installations/123/access_tokens":
            assert token == "synthetic-jwt" and method == "POST" and body == {}
            return 201, {"token": "synthetic-installation", "permissions": self.perms}
        assert token == "synthetic-installation"
        if path == "/graphql":
            assert method == "POST" and body["variables"] == {"number": self.number}
            return 200, {"data": {"repository": deepcopy(self.graph)}}
        assert method == "GET"
        suffix = path.removeprefix(self.base)
        if suffix == f"pulls/{self.number}":
            return 200, deepcopy(self.pr_sequence.pop(0) if self.pr_sequence else self.pr)
        if suffix == f"issues/{self.number}/timeline?per_page=100":
            return 200, deepcopy(self.timeline)
        if suffix == "git/ref/heads/main":
            return 200, {"ref": "refs/heads/main", "object": {
                "type": "commit", "sha": self.refs.pop(0) if self.refs else self.main}}
        if suffix == f"git/commits/{SHA}":
            return 200, {"sha": SHA, "tree": {"sha": self.tree}, "parents": [{"sha": p} for p in self.parents]}
        if suffix == f"git/commits/{HEAD}":
            return 200, {"sha": HEAD, "tree": {"sha": TREE}, "parents": [{"sha": BASE}]}
        if suffix.startswith(f"compare/{SHA}..."):
            tip = suffix.split("...")[1].split("?")[0]
            return 200, deepcopy(self.relation or {"status": "identical" if tip == SHA else "ahead",
                                                   "merge_base_commit": {"sha": SHA}})
        if suffix == "rules/branches/main":
            return 200, deepcopy(self.rules)
        if suffix == f"commits/{HEAD}/check-runs?filter=all&per_page=100":
            return 200, {"total_count": len(self.checks), "check_runs": deepcopy(self.checks)}
        pytest.fail(f"unexpected endpoint {path}")

    def credentials(self, monkeypatch):
        config = {"GITHUB_APP_ID": str(evidence.APP_ID), "GITHUB_APP_INSTALLATION_ID": "123",
                  "GITHUB_APP_CLIENT_ID": "fixture", "GITHUB_APP_PRIVATE_KEY_PATH": "/fixture"}
        monkeypatch.setattr(evidence, "load_env", lambda _: config)
        monkeypatch.setattr(evidence, "make_jwt", lambda *_: "synthetic-jwt")


@pytest.mark.parametrize(("case", "outcome"), [
    ("merge", "PASS"), ("squash", "PASS"), ("rebase", "PASS"), ("main_advanced", "PASS"),
    ("null_rest_valid", "PASS"), ("omitted_rest_valid", "PASS"), ("null_rest_no_timeline", "INSUFFICIENT"),
    ("rest_conflicts", "CONTRADICTION"), ("timeline_conflicts", "CONTRADICTION"),
    ("closed_without_merge", "CONTRADICTION"), ("open", "CONTRADICTION"),
    ("wrong_repo", "CONTRADICTION"), ("wrong_base", "CONTRADICTION"),
    ("head_as_target", "CONTRADICTION"), ("not_ancestor", "CONTRADICTION"),
    ("other_pr_commit", "CONTRADICTION"), ("wrong_tree", "CONTRADICTION"),
    ("wrong_parent", "CONTRADICTION"), ("auth_failure", "AUTHENTICATION"),
    ("wrong_app", "AUTHORIZATION"), ("insufficient_permission", "AUTHORIZATION"),
    ("partial", "INSUFFICIENT"), ("timeout", "TIMEOUT"), ("rate_limit", "RATE_LIMIT"),
    ("transient", "INFRASTRUCTURE"), ("absent_endpoint", "UNAVAILABLE"),
    ("changed_pr", "CONTRADICTION"), ("changed_main", "CONTRADICTION"),
    ("wrong_ruleset", "AUTHORIZATION"), ("old_success", "CONTRADICTION"),
])
def test_integration_evidence(case, outcome, monkeypatch):
    api = GitHub()
    api.credentials(monkeypatch)
    target = SHA
    if case in {"squash", "rebase"}:
        api.parents = [BASE if case == "squash" else NEXT]
    if case == "main_advanced":
        api.main = NEXT
    if case.startswith("null_rest") or case == "other_pr_commit":
        api.pr["merge_commit_sha"] = None
    if case == "omitted_rest_valid":
        del api.pr["merge_commit_sha"]
    if case == "null_rest_no_timeline":
        api.timeline = []
    if case == "rest_conflicts":
        api.pr["merge_commit_sha"] = NEXT
    if case == "timeline_conflicts":
        api.timeline.append({"event": "merged", "commit_id": NEXT})
    if case == "closed_without_merge":
        api.pr["merged"] = False
    if case == "open":
        api.pr["state"] = "open"
    if case == "wrong_repo":
        api.pr["head"]["repo"]["full_name"] = "other/project"
    if case == "wrong_base":
        api.pr["base"]["ref"] = "dev"
    if case == "head_as_target":
        target = HEAD
    if case == "not_ancestor":
        api.main = NEXT
        api.relation = {"status": "diverged", "merge_base_commit": {"sha": BASE}}
    if case == "other_pr_commit":
        api.graph["pullRequest"]["mergeCommit"]["oid"] = NEXT
    if case == "wrong_tree":
        api.tree = NEXT
    if case == "wrong_parent":
        api.parents = [BASE, NEXT]
    if case == "auth_failure":
        api.failures["/app"] = (401, {})
    if case == "wrong_app":
        api.app_id = 123
    if case == "insufficient_permission":
        api.perms["checks"] = "read"
    if case == "partial":
        del api.graph["pullRequest"]["headRefOid"]
    if case in {"timeout", "rate_limit", "transient", "absent_endpoint"}:
        api.failures["/graphql"] = {
            "timeout": TimeoutError(), "rate_limit": (429, {}),
            "transient": (503, {}), "absent_endpoint": (404, {}),
        }[case]
    if case == "changed_pr":
        api.pr_sequence = [api.pr, {**deepcopy(api.pr), "merge_commit_sha": NEXT}]
    if case == "changed_main":
        api.refs = [SHA, NEXT]
    if case == "wrong_ruleset":
        api.rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = 1
    if case == "old_success":
        api.checks.append({**api.checks[0], "id": 9, "conclusion": "failure"})
    if outcome == "PASS":
        result = evidence.validate(evidence.authenticate(api.transport), REPO, api.number, target)
        assert (result.commit, result.head, result.tree, result.checks) == (SHA, HEAD, TREE, (1, 2))
        assert result.strategy == ("linear_integration" if case in {"squash", "rebase"} else "merge_commit")
    else:
        with pytest.raises(evidence.EvidenceError, match=f"^{outcome}:"):
            evidence.validate(evidence.authenticate(api.transport), REPO, api.number, target)


@pytest.mark.parametrize("suite", ["python", "postgres"])
def test_preflight_only_reads_with_real_authority_contract(monkeypatch, capsys, suite):
    api = GitHub()
    api.pr["merge_commit_sha"] = None
    api.credentials(monkeypatch)
    name = "distributed_shadow_checks" if suite == "python" else "distributed_postgres_check"
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "request_json", api.transport)
    for operation in ("ensure_tables", "create_check", "complete_check", "run_command"):
        monkeypatch.setattr(module, operation, lambda *a, **kw: pytest.fail("preflight attempted a side effect"))
    args = ["publisher", "run", "--merged-main", "--preflight", "--pr", "445", "--sha", SHA]
    monkeypatch.setattr(sys, "argv", args + (["--suite", "python"] if suite == "python" else []))
    module.main()
    assert "PREFLIGHT=PASS" in capsys.readouterr().out
    assert {path for path, method in api.calls if method == "POST"} == {
        "/graphql", "/app/installations/123/access_tokens",
    }


@pytest.mark.parametrize(("remote_state", "conclusion"), [
    ("completed", "success"), ("completed", "failure"), ("in_progress", None),
])
def test_lost_completion_response_is_reconciled_without_another_write(remote_state, conclusion):
    calls = []

    def api(path, token):
        assert (path, token) == ("/repos/" + REPO + "/check-runs/91", "synthetic")
        calls.append("GET")
        return 200, {"id": 91, "head_sha": SHA, "name": "distributed-python",
                     "status": remote_state, "conclusion": conclusion, "app": {"id": evidence.APP_ID}}

    def publish():
        calls.append("PATCH")
        raise TimeoutError()

    session = evidence.AppSession(api, "synthetic")
    if remote_state == "completed" and conclusion == "success":
        evidence.finalize(session, 91, SHA, "distributed-python", "success", publish)
    else:
        with pytest.raises(evidence.PublicationUncertain, match="COMPLETION_UNRECONCILED"):
            evidence.finalize(session, 91, SHA, "distributed-python", "success", publish)
    assert calls == ["PATCH", "GET"]
