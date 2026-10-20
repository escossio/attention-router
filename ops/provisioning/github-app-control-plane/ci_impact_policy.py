"""Fail-closed, path-aware CI eligibility shared by GitHub Actions and App publishers.

The GitHub App loads this file from its trusted installed default-branch code;
untrusted PR code cannot decide which distributed checks are applicable.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Callable

SUITES = ("python", "transport", "docker", "postgres")
REPO = "escossio/attention-router"
ALLOWED_STATUSES = {"added", "modified", "removed", "renamed", "copied", "changed"}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DOCS_ONLY = ("docs/",)
_FULL = frozenset(SUITES)
_MAX_FILES = 2999  # GitHub PR file-list hard cap: an exact 3000 is incomplete.


@dataclass(frozen=True)
class Impact:
    suites: frozenset[str]
    reason: str
    paths_sha256: str
    file_count: int
    policy: str = "ci-impact-v1"

    def required(self, suite: str) -> bool:
        return suite in self.suites

    def summary(self, suite: str) -> str:
        decision = "RUN" if self.required(suite) else "NOT_APPLICABLE"
        return (f"CI_IMPACT={decision}; suite={suite}; policy={self.policy}; "
                f"files={self.file_count}; paths_sha256={self.paths_sha256}; "
                f"reason={self.reason}. Only paths in the exact GitHub PR/commit diff are evaluated.")


def fail_closed(reason: str) -> Impact:
    return Impact(_FULL, f"FAIL_CLOSED:{reason}", "unavailable", 0)


def _suites_for_path(path: str) -> frozenset[str]:
    # CI/security/provisioning/policy changes ALWAYS run all tests.
    if (path.startswith(".github/") or path.startswith("ops/provisioning/")
            or path.startswith("scripts/ci_") or path.startswith("scripts/public_secret")
            or path.startswith("ops/provisioning/github-app-control-plane/")):
        return _FULL
    if path == "STATUS.md" or path == "LICENSE" or path == "LICENSE.md":
        return frozenset()
    if path.startswith(_DOCS_ONLY):
        # This JS is checked by transport-tests; its syntax is a real CI contract.
        if path == "docs/public/cognitive-architecture/app.js":
            return frozenset({"transport"})
        # Documentation can contain executable scripts; do not exempt those.
        if path.endswith((".py", ".sh", ".js", ".ts", ".mjs", ".cjs", ".yml", ".yaml")):
            return _FULL
        return frozenset()
    if path == "README.md":  # copied directly into the runtime Docker image
        return frozenset({"docker"})
    if path.startswith("whatsapp-transport-local/test/"):
        return frozenset({"transport"})
    if path.startswith("whatsapp-transport-local/"):
        return frozenset({"transport", "docker"})
    if path.startswith("ops/whatsapp-container/"):
        return frozenset({"transport", "docker", "python"})
    if path.startswith("alembic/") or path == "alembic.ini":
        return frozenset({"python", "postgres", "docker"})
    if path.startswith("tests/integration/"):
        return frozenset({"python", "postgres"})
    if path.startswith("tests/"):
        return frozenset({"python"})
    if path.startswith("attention_router/"):
        # Most code shares auth/tenant/DB execution contracts. Full is safer.
        return _FULL
    # Unknown, renamed into an unfamiliar subtree, build scripts, SDKs, deps.
    return _FULL


def classify_files(files: list[dict] | None) -> Impact:
    if not isinstance(files, list) or not files or len(files) > _MAX_FILES:
        return fail_closed("empty_invalid_or_truncated_diff")
    normalized: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or row.get("status") not in ALLOWED_STATUSES:
            return fail_closed("invalid_file_metadata")
        paths = [row.get("filename")]
        if row["status"] in {"renamed", "copied"}:
            paths.append(row.get("previous_filename"))
        for path in paths:
            if (not isinstance(path, str) or not path or path.startswith("/")
                    or "\\" in path or "\x00" in path or ".." in path.split("/")):
                return fail_closed("unsafe_or_missing_filename")
            normalized.add(path)
    suites: set[str] = set()
    for path in normalized:
        suites.update(_suites_for_path(path))
    fingerprint = sha256(json.dumps(sorted(normalized), separators=(",", ":")).encode()).hexdigest()
    reason = "FULL_IMPACT" if suites == set(SUITES) else "PATH_SCOPED"
    if not suites:
        reason = "NON_EXECUTABLE_DOCS_ONLY"
    return Impact(frozenset(suites), reason, fingerprint, len(normalized))


def get_pr_impact(
    repo: str, number: int, head_sha: str, request: Callable[[str], tuple[int, object]],
    *, base_branch: str = "main",
) -> Impact:
    """Fetch a complete, exact-head GitHub PR diff; any uncertainty runs everything."""
    if repo != REPO or not isinstance(number, int) or number < 1 or not _SHA.fullmatch(head_sha):
        return fail_closed("invalid_pr_identity")
    try:
        status, pr = request(f"/repos/{repo}/pulls/{number}")
        if (status != 200 or not isinstance(pr, dict)
                or pr.get("state") != "open"
                or (pr.get("head") or {}).get("sha") != head_sha
                or (pr.get("base") or {}).get("ref") != base_branch
                or (pr.get("head") or {}).get("repo", {}).get("full_name") != repo):
            return fail_closed("pr_head_base_or_repo_mismatch")
        expected = pr.get("changed_files")
        if not isinstance(expected, int) or expected < 1 or expected > _MAX_FILES:
            return fail_closed("invalid_pr_file_count")
        files: list[dict] = []
        for page in range(1, 32):
            status, batch = request(f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
            if status != 200 or not isinstance(batch, list) or len(batch) > 100:
                return fail_closed("pr_files_api_unavailable")
            files.extend(batch)
            if len(files) > _MAX_FILES:
                return fail_closed("pr_files_truncated")
            if len(batch) < 100:
                break
        if len(files) != expected:
            return fail_closed("pr_files_count_mismatch")
        return classify_files(files)
    except Exception:
        return fail_closed("pr_diff_fetch_exception")


def classify_compare(before: str, after: str, response: object) -> Impact:
    """Push range via compare endpoint; GitHub limits files to 300."""
    if not _SHA.fullmatch(before) or not _SHA.fullmatch(after) or before == after:
        return fail_closed("invalid_push_range")
    if not isinstance(response, dict) or response.get("status") != "ahead":
        return fail_closed("push_not_linear")
    files = response.get("files")
    count = response.get("total_commits")
    if not isinstance(count, int) or count < 1 or count > 250:
        return fail_closed("push_commit_count_unknown")
    if not isinstance(files, list) or len(files) >= 300:
        return fail_closed("push_files_truncated")
    return classify_files(files)
