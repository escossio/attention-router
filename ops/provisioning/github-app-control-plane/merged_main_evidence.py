"""Authenticated evidence for certifying an integrated, previously tested tree."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field as private_field
import re
from urllib.error import HTTPError, URLError

from ci_impact_policy import REPO
from probe import CONFIG, INSTALLATION_CONFIG, load_env, make_jwt

APP_ID = 4982054
APP_SLUG = "andy-github-control-plane"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_INTEGRATION_CHECKS = {"distributed-python", "distributed-postgres"}
_MERGE_QUERY = """
query MergedMain($number: Int!) {
  repository(owner: "escossio", name: "attention-router") {
    nameWithOwner
    pullRequest(number: $number) {
      number state merged baseRefName headRefOid mergeCommit { oid }
    }
  }
}
"""


class EvidenceError(ValueError):
    """A stable diagnostic that contains neither credentials nor remote bodies."""

    def __init__(self, category, source):
        super().__init__(f"{category}:{source}")


def _require(condition, category, source):
    if not condition:
        raise EvidenceError(category, source)


def _object(value, source):
    _require(isinstance(value, dict), "INSUFFICIENT", source)
    return value


def _sha(value, source):
    _require(isinstance(value, str) and _SHA.fullmatch(value), "INSUFFICIENT", source)
    return value


def _expect(value, expected, source):
    _require(value is not None, "INSUFFICIENT", source)
    _require(type(value) is type(expected) and value == expected, "CONTRADICTION", source)


def _exchange(transport, path, token, source, *, status=200, body=None):
    try:
        result, payload = transport(path, token, **({"method": "POST", "body": body} if body is not None else {}))
    except HTTPError as error:
        result, payload = error.code, None
        headers = error.headers or {}
        if result == 403 and (headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")):
            result = 429
        error.close()
    except TimeoutError:
        raise EvidenceError("TIMEOUT", source) from None
    except (URLError, OSError) as error:
        category = "TIMEOUT" if isinstance(getattr(error, "reason", None), TimeoutError) else "INFRASTRUCTURE"
        raise EvidenceError(category, source) from None
    except ValueError:
        raise EvidenceError("INSUFFICIENT", source) from None
    if result != status:
        category = {401: "AUTHENTICATION", 403: "AUTHORIZATION", 404: "UNAVAILABLE", 429: "RATE_LIMIT"}.get(
            result, "INFRASTRUCTURE" if result >= 500 else "UNAVAILABLE",
        )
        raise EvidenceError(category, source)
    return payload


@dataclass(frozen=True)
class AppSession:
    transport: Callable = private_field(repr=False)
    token: str = private_field(repr=False)

    def read(self, suffix, source):
        return _exchange(self.transport, f"/repos/{REPO}/{suffix}", self.token, source)

    def graphql(self, number):
        data = _object(_exchange(self.transport, "/graphql", self.token, "graphql", body={
            "query": _MERGE_QUERY, "variables": {"number": number},
        }), "graphql")
        if data.get("errors"):
            kinds = {str(error.get("type", "")) for error in data["errors"] if isinstance(error, dict)}
            category = "INSUFFICIENT"
            for kind, meaning in (("RATE_LIMITED", "RATE_LIMIT"), ("UNAUTHENTICATED", "AUTHENTICATION"),
                                  ("FORBIDDEN", "AUTHORIZATION"), ("INTERNAL", "INFRASTRUCTURE")):
                if kind in kinds:
                    category = meaning
                    break
            raise EvidenceError(category, "graphql")
        return _object(_object(data.get("data"), "graphql").get("repository"), "graphql")


def authenticate(transport):
    try:
        cfg, install = load_env(CONFIG), load_env(INSTALLATION_CONFIG)
        configured = int(cfg["GITHUB_APP_ID"])
        installation = int(install["GITHUB_APP_INSTALLATION_ID"])
        jwt = make_jwt(cfg["GITHUB_APP_CLIENT_ID"], cfg["GITHUB_APP_PRIVATE_KEY_PATH"])
    except (KeyError, OSError, TypeError, ValueError):
        raise EvidenceError("AUTHENTICATION", "app_configuration") from None
    _require(configured == APP_ID, "AUTHORIZATION", "configured_app")
    app = _object(_exchange(transport, "/app", jwt, "app_identity"), "app_identity")
    _require(app.get("id") == APP_ID and app.get("slug") == APP_SLUG, "AUTHORIZATION", "app_identity")
    scope = _object(_exchange(transport, f"/repos/{REPO}/installation", jwt, "installation"), "installation")
    _require(scope.get("id") == installation and "suspended_at" in scope and scope["suspended_at"] is None,
             "AUTHORIZATION", "installation")
    access = _object(_exchange(transport, f"/app/installations/{installation}/access_tokens", jwt,
                              "installation_token", status=201, body={}), "installation_token")
    perms = _object(access.get("permissions"), "installation_permissions")
    _require(perms.get("checks") == "write" and all(
        perms.get(name) in {"read", "write"} for name in ("contents", "pull_requests", "issues", "metadata")
    ), "AUTHORIZATION", "installation_permissions")
    token = access.get("token")
    _require(isinstance(token, str) and bool(token), "AUTHENTICATION", "installation_token")
    return AppSession(transport, token)


@dataclass(frozen=True)
class Integration:
    number: int
    head: str
    commit: str
    tree: str
    strategy: str
    checks: tuple[int, ...]


def _pull(session, number, sha):
    source = "rest_pr"
    pr = _object(session.read(f"pulls/{number}", source), source)
    _expect(pr.get("number"), number, source)
    _expect(pr.get("state"), "closed", source)
    _expect(pr.get("merged"), True, source)
    for side in ("base", "head"):
        identity = _object(pr.get(side), source)
        _expect(_object(identity.get("repo"), source).get("full_name"), REPO, source)
    _expect(pr["base"].get("ref"), "main", source)
    head = _sha(pr["head"].get("sha"), source)
    _require(sha != head, "CONTRADICTION", "head_is_integration")
    legacy_sha = pr.get("merge_commit_sha")
    if legacy_sha is not None:
        _expect(legacy_sha, sha, source)
    return head, legacy_sha


def _timeline(session, number, sha, legacy_sha):
    found = False
    for page in range(1, 11):
        suffix = "?per_page=100" + (f"&page={page}" if page > 1 else "")
        batch = session.read(f"issues/{number}/timeline{suffix}", "timeline")
        _require(isinstance(batch, list) and len(batch) <= 100, "INSUFFICIENT", "timeline")
        for item in batch:
            item = _object(item, "timeline")
            if item.get("event") == "merged" and item.get("commit_id") is not None:
                _expect(item["commit_id"], sha, "timeline")
                found = True
        if len(batch) < 100:
            _require(found or legacy_sha is not None, "INSUFFICIENT", "pr_commit_link")
            return
    raise EvidenceError("INSUFFICIENT", "timeline_pagination")


def _main(session):
    ref = _object(session.read("git/ref/heads/main", "main_ref"), "main_ref")
    _expect(ref.get("ref"), "refs/heads/main", "main_ref")
    obj = _object(ref.get("object"), "main_ref")
    _expect(obj.get("type"), "commit", "main_ref")
    return _sha(obj.get("sha"), "main_ref")


def _commit(session, sha):
    raw = _object(session.read(f"git/commits/{sha}", "git_commit"), "git_commit")
    _expect(raw.get("sha"), sha, "git_commit")
    tree = _sha(_object(raw.get("tree"), "git_tree").get("sha"), "git_tree")
    parents = raw.get("parents")
    _require(isinstance(parents, list), "INSUFFICIENT", "git_parents")
    return tree, tuple(_sha(_object(p, "git_parents").get("sha"), "git_parents") for p in parents)


def _head_checks(session, head):
    rules = session.read("rules/branches/main", "rulesets")
    _require(isinstance(rules, list), "INSUFFICIENT", "rulesets")
    required = set()
    for rule in rules:
        rule = _object(rule, "rulesets")
        if rule.get("type") != "required_status_checks":
            continue
        rows = _object(rule.get("parameters"), "rulesets").get("required_status_checks")
        _require(isinstance(rows, list), "INSUFFICIENT", "rulesets")
        for row in rows:
            row = _object(row, "rulesets")
            name, app = row.get("context"), row.get("integration_id")
            _require(isinstance(name, str) and name and type(app) is int and app > 0, "INSUFFICIENT", "rulesets")
            required.add((name, app))
    _require({(name, APP_ID) for name in _INTEGRATION_CHECKS} <= required, "AUTHORIZATION", "rulesets")
    _require(all(name not in _INTEGRATION_CHECKS or app == APP_ID for name, app in required),
             "CONTRADICTION", "rulesets")
    data = _object(session.read(f"commits/{head}/check-runs?filter=all&per_page=100", "head_checks"), "head_checks")
    rows = data.get("check_runs")
    _require(isinstance(rows, list) and data.get("total_count") == len(rows) <= 100, "INSUFFICIENT", "head_checks")
    latest = {}
    for row in rows:
        row = _object(row, "head_checks")
        name, app = row.get("name"), _object(row.get("app"), "head_checks").get("id")
        _require(isinstance(name, str) and type(app) is int, "INSUFFICIENT", "head_checks")
        key = name, app
        if key not in required:
            continue
        _expect(row.get("head_sha"), head, "head_checks")
        check_id = row.get("id")
        _require(type(check_id) is int and check_id > 0, "INSUFFICIENT", "head_checks")
        if key not in latest or check_id > latest[key]["id"]:
            latest[key] = row
    _require(latest.keys() == required, "INSUFFICIENT", "head_checks")
    for row in latest.values():
        _expect(row.get("status"), "completed", "head_checks")
        _expect(row.get("conclusion"), "success", "head_checks")
    return tuple(sorted(row["id"] for row in latest.values()))


def validate(session, repo, number, sha):
    _require(repo == REPO and type(number) is int and number > 0, "CONTRADICTION", "requested_pr")
    _sha(sha, "requested_sha")
    main = _main(session)
    head, legacy_sha = _pull(session, number, sha)
    repository = session.graphql(number)
    _expect(repository.get("nameWithOwner"), repo, "graphql")
    pull = _object(repository.get("pullRequest"), "graphql")
    for field, expected in (("number", number), ("merged", True), ("state", "MERGED"),
                            ("baseRefName", "main"), ("headRefOid", head)):
        _expect(pull.get(field), expected, "graphql")
    _expect(_object(pull.get("mergeCommit"), "graphql").get("oid"), sha, "graphql")
    _timeline(session, number, sha, legacy_sha)

    tree, parents = _commit(session, sha)
    head_tree, _ = _commit(session, head)
    _expect(tree, head_tree, "tree_identity")
    _require(len(set(parents)) == len(parents) and sha not in parents, "CONTRADICTION", "git_parents")
    if len(parents) == 2:
        _expect(parents[1], head, "merge_parent")
        strategy = "merge_commit"
    else:
        _require(len(parents) == 1 and head not in parents, "CONTRADICTION", "linear_merge")
        strategy = "linear_integration"

    relation = _object(session.read(f"compare/{sha}...{main}?per_page=1", "ancestry"), "ancestry")
    _expect(relation.get("status"), "identical" if sha == main else "ahead", "ancestry")
    _expect(_object(relation.get("merge_base_commit"), "ancestry").get("sha"), sha, "ancestry")
    checks = _head_checks(session, head)
    _expect(_pull(session, number, sha), (head, legacy_sha), "pr_changed_during_validation")
    _expect(_main(session), main, "main_changed_during_validation")
    return Integration(number, head, sha, tree, strategy, checks)


class PublicationUncertain(RuntimeError):
    """The official remote state must be reconciled before issuing another check."""


def finalize(session, check_id, sha, name, conclusion, publish):
    """Resolve an ambiguous completion via a read, without overwriting a terminal result."""
    try:
        publish()
    except (OSError, RuntimeError, ValueError):
        try:
            remote = _object(session.read(f"check-runs/{check_id}", "completion"), "completion")
            for field, expected in (
                ("id", check_id), ("head_sha", sha), ("name", name),
                ("status", "completed"), ("conclusion", conclusion),
            ):
                _expect(remote.get(field), expected, "completion")
            app = _object(remote.get("app"), "completion")
            _expect(app.get("id"), APP_ID, "completion")
        except EvidenceError:
            raise PublicationUncertain("MERGED_MAIN_CHECK_COMPLETION_UNRECONCILED") from None
