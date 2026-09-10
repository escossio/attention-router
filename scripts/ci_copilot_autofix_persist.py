from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import ci_agent
from scripts import ci_copilot_advisor as advisor
from scripts import ci_copilot_autofix_proposal as autofix
from scripts import ci_copilot_patch_trial as trial


class AutofixPersistError(RuntimeError):
    pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic persistent Copilot autofix V2")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--proposal-b64", required=True)
    parser.add_argument("--candidate-dir", required=True)
    return parser.parse_args(argv)


def _decode_artifact(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - boundary parser deliberately normalizes all decode failures
        raise AutofixPersistError("autofix proposal artifact is not valid base64 JSON") from exc
    if not isinstance(payload, dict):
        raise AutofixPersistError("autofix proposal artifact must be an object")
    return payload


def _validate_branch_name(branch: str) -> None:
    if not branch.startswith(autofix.AUTOFIX_BRANCH_PREFIX):
        raise AutofixPersistError("branch is not opted in for persistent autofix")
    if branch.startswith("/") or branch.endswith("/") or ".." in branch or "//" in branch:
        raise AutofixPersistError("branch name is structurally unsafe")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/")
    if not branch or any(char not in allowed for char in branch):
        raise AutofixPersistError("branch name contains unsupported characters")


def _fetch_pr(repo: str, pr_number: int, *, token: str) -> dict[str, Any]:
    return ci_agent._request_json("GET", ci_agent._api_url(repo, f"pulls/{pr_number}"), token=token)


def _validate_live_pr(
    repo: str,
    pr_number: int,
    *,
    source_head: str,
    branch: str,
    token: str,
) -> None:
    pr = _fetch_pr(repo, pr_number, token=token)
    if str(pr.get("state") or "") != "open":
        raise AutofixPersistError("pull request is no longer open")
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    if str(head_repo.get("full_name") or "") != repo:
        raise AutofixPersistError("cross-repository pull requests are not eligible")
    if str(head.get("ref") or "") != branch:
        raise AutofixPersistError("pull request branch changed")
    if str(head.get("sha") or "") != source_head:
        raise AutofixPersistError("pull request head changed")


def _validate_artifact(
    artifact: dict[str, Any],
    *,
    repo: str,
    pr_number: int,
    source_head: str,
    branch: str,
    changed_files: tuple[str, ...],
) -> tuple[trial.PatchProposal, str]:
    expected = {
        "schema",
        "repo",
        "pr_number",
        "source_head",
        "branch",
        "trigger_workflow",
        "trigger_run_id",
        "patch_sha256",
        "log_excerpt",
        "patch_proposal",
    }
    if set(artifact) != expected:
        raise AutofixPersistError("autofix artifact keys do not match V2 contract")
    if artifact.get("schema") != autofix.AUTOFIX_PROPOSAL_SCHEMA:
        raise AutofixPersistError("autofix artifact schema mismatch")
    if artifact.get("repo") != repo or int(artifact.get("pr_number") or 0) != pr_number:
        raise AutofixPersistError("autofix artifact repository identity mismatch")
    if artifact.get("source_head") != source_head or artifact.get("branch") != branch:
        raise AutofixPersistError("autofix artifact source identity mismatch")
    if artifact.get("trigger_workflow") != "Public CI":
        raise AutofixPersistError("persistent autofix is restricted to Public CI failures")

    patch_payload = artifact.get("patch_proposal")
    if not isinstance(patch_payload, dict):
        raise AutofixPersistError("autofix artifact patch proposal is invalid")
    patchable = trial.patchable_changed_files(changed_files)
    proposal = trial.validate_patch_payload(patch_payload, patchable_files=patchable)
    if proposal.decision != "PATCH" or proposal.confidence != "HIGH":
        raise AutofixPersistError("autofix artifact is not a high-confidence patch")

    patch_hash = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
    if patch_hash != artifact.get("patch_sha256"):
        raise AutofixPersistError("autofix artifact patch hash mismatch")
    log_excerpt = str(artifact.get("log_excerpt") or "")
    if len(log_excerpt) > 20_000:
        raise AutofixPersistError("autofix artifact log excerpt exceeds bound")
    return proposal, log_excerpt


def _candidate_head(candidate_dir: Path) -> str:
    return trial._run(["git", "rev-parse", "HEAD"], cwd=candidate_dir, timeout=20).stdout.strip()


def _tracked_mode(candidate_dir: Path, path: str) -> str:
    result = trial._run(["git", "ls-files", "-s", "--", path], cwd=candidate_dir, timeout=20)
    parts = result.stdout.strip().split(maxsplit=3)
    if result.returncode != 0 or len(parts) < 4:
        raise AutofixPersistError(f"cannot resolve tracked mode for {path}")
    mode = parts[0]
    if mode not in {"100644", "100755"}:
        raise AutofixPersistError(f"unsupported tracked mode for {path}")
    return mode


def materialize_validated_contents(
    proposal: trial.PatchProposal,
    *,
    candidate_dir: Path,
) -> tuple[dict[str, str], dict[str, str], bool]:
    modes = {path: _tracked_mode(candidate_dir, path) for path in proposal.target_files}
    contents: dict[str, str] = {}
    clean = False
    try:
        checked = trial._run(
            ["git", "apply", "--check", "--whitespace=error-all", "-"],
            cwd=candidate_dir,
            input_text=proposal.patch,
            timeout=20,
        )
        if checked.returncode != 0:
            raise AutofixPersistError("validated patch no longer passes git apply --check")
        applied = trial._run(
            ["git", "apply", "--whitespace=error-all", "-"],
            cwd=candidate_dir,
            input_text=proposal.patch,
            timeout=20,
        )
        if applied.returncode != 0:
            raise AutofixPersistError("validated patch could not be materialized")
        names = trial._run(["git", "diff", "--name-only"], cwd=candidate_dir, timeout=20)
        actual = tuple(line.strip() for line in names.stdout.splitlines() if line.strip())
        if set(actual) != set(proposal.target_files):
            raise AutofixPersistError("materialized patch changed unexpected files")
        diff_check = trial._run(["git", "diff", "--check"], cwd=candidate_dir, timeout=20)
        if diff_check.returncode != 0:
            raise AutofixPersistError("materialized patch failed git diff --check")
        for path in proposal.target_files:
            file_path = candidate_dir / path
            if not file_path.is_file():
                raise AutofixPersistError(f"materialized target disappeared: {path}")
            text = file_path.read_text(encoding="utf-8")
            if len(text) > 250_000:
                raise AutofixPersistError(f"materialized target exceeds content bound: {path}")
            contents[path] = text
    finally:
        trial._run(["git", "reset", "--hard", "HEAD"], cwd=candidate_dir, timeout=20)
        trial._run(["git", "clean", "-fdx"], cwd=candidate_dir, timeout=20)
        status = trial._run(["git", "status", "--porcelain"], cwd=candidate_dir, timeout=20)
        clean = status.returncode == 0 and not status.stdout.strip()
    return contents, modes, clean


def _create_commit_from_contents(
    repo: str,
    *,
    source_head: str,
    branch: str,
    contents: dict[str, str],
    modes: dict[str, str],
    patch_sha256: str,
    token: str,
) -> str:
    base_commit = ci_agent._request_json(
        "GET", ci_agent._api_url(repo, f"git/commits/{source_head}"), token=token
    )
    base_tree = str((base_commit.get("tree") or {}).get("sha") or "")
    if not base_tree:
        raise AutofixPersistError("source commit tree is unavailable")

    tree_entries: list[dict[str, str]] = []
    for path, content in contents.items():
        blob = ci_agent._request_json(
            "POST",
            ci_agent._api_url(repo, "git/blobs"),
            token=token,
            payload={"content": content, "encoding": "utf-8"},
        )
        blob_sha = str(blob.get("sha") or "")
        if not blob_sha:
            raise AutofixPersistError(f"blob creation failed for {path}")
        tree_entries.append({"path": path, "mode": modes[path], "type": "blob", "sha": blob_sha})

    tree = ci_agent._request_json(
        "POST",
        ci_agent._api_url(repo, "git/trees"),
        token=token,
        payload={"base_tree": base_tree, "tree": tree_entries},
    )
    tree_sha = str(tree.get("sha") or "")
    if not tree_sha:
        raise AutofixPersistError("autofix tree creation failed")

    message = (
        "ci-agent: apply validated autofix\n\n"
        "CI-Agent-Version: v2\n"
        f"CI-Agent-Source-Head: {source_head}\n"
        f"CI-Agent-Patch-SHA256: {patch_sha256}\n"
        "CI-Agent-Merge: disabled"
    )
    commit = ci_agent._request_json(
        "POST",
        ci_agent._api_url(repo, "git/commits"),
        token=token,
        payload={"message": message, "tree": tree_sha, "parents": [source_head]},
    )
    commit_sha = str(commit.get("sha") or "")
    if not commit_sha:
        raise AutofixPersistError("autofix commit creation failed")

    ref_path = f"git/refs/heads/{branch}"
    ci_agent._request_json(
        "PATCH",
        ci_agent._api_url(repo, ref_path),
        token=token,
        payload={"sha": commit_sha, "force": False},
    )
    return commit_sha


def _render_persisted_comment(
    *,
    pr_number: int,
    source_head: str,
    commit_sha: str,
    patch_sha256: str,
    target_files: tuple[str, ...],
) -> str:
    return "\n".join(
        [
            autofix.AUTOFIX_MARKER,
            "### Copilot Autofix V2 ✅ PERSISTED",
            "",
            f"PR: `#{pr_number}` · source head: `{source_head[:12]}` · agent commit: `{commit_sha[:12]}`",
            f"Updated: `{datetime.now(UTC).isoformat()}`",
            "",
            "**State:** `PERSISTED`",
            f"**Attempt budget:** `1/{autofix.MAX_PERSISTENT_ATTEMPTS}` used — further V2 writes on this PR are blocked.",
            "**Authority split:** Copilot produced a read-only proposal; a separate deterministic job revalidated and persisted it.",
            "**Merge:** `DISABLED`",
            "",
            "**Files:** " + ", ".join(f"`{path}`" for path in target_files),
            f"**Patch SHA-256:** `{patch_sha256}`",
            "**Persistence:** single Git commit through GitHub Git Data API, non-force branch ref update.",
            "**Next:** normal Public CI/CodeQL must validate the new head. No automatic merge will occur.",
        ]
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    try:
        _validate_branch_name(args.branch)
        _validate_live_pr(
            args.repo,
            args.pr_number,
            source_head=args.source_head,
            branch=args.branch,
            token=token,
        )
        if autofix.persistent_attempt_used(args.repo, args.pr_number, token=token):
            raise AutofixPersistError("persistent attempt budget is already exhausted")

        artifact = _decode_artifact(args.proposal_b64)
        changed_files = advisor._fetch_changed_files(args.repo, args.pr_number, token=token)
        proposal, log_excerpt = _validate_artifact(
            artifact,
            repo=args.repo,
            pr_number=args.pr_number,
            source_head=args.source_head,
            branch=args.branch,
            changed_files=changed_files,
        )

        candidate_dir = Path(args.candidate_dir).resolve()
        if not candidate_dir.is_dir() or not (candidate_dir / ".git").exists():
            raise AutofixPersistError("candidate-dir must be an exact-head git checkout")
        if _candidate_head(candidate_dir) != args.source_head:
            raise AutofixPersistError("candidate checkout is not the exact source head")

        validation = trial.apply_and_validate_ephemerally(
            proposal,
            candidate_dir=candidate_dir,
            log_excerpt=log_excerpt,
        )
        if validation.status != "VALIDATED_PASS" or not validation.cleanup_clean:
            raise AutofixPersistError("deterministic persistence revalidation failed")

        contents, modes, clean = materialize_validated_contents(proposal, candidate_dir=candidate_dir)
        if not clean:
            raise AutofixPersistError("candidate workspace was not clean after materialization")

        _validate_live_pr(
            args.repo,
            args.pr_number,
            source_head=args.source_head,
            branch=args.branch,
            token=token,
        )
        if autofix.persistent_attempt_used(args.repo, args.pr_number, token=token):
            raise AutofixPersistError("persistent attempt budget changed before commit")

        patch_sha = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
        commit_sha = _create_commit_from_contents(
            args.repo,
            source_head=args.source_head,
            branch=args.branch,
            contents=contents,
            modes=modes,
            patch_sha256=patch_sha,
            token=token,
        )
        body = _render_persisted_comment(
            pr_number=args.pr_number,
            source_head=args.source_head,
            commit_sha=commit_sha,
            patch_sha256=patch_sha,
            target_files=proposal.target_files,
        )
        comment_id = autofix._upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
    except (AutofixPersistError, trial.PatchTrialError, advisor.AdvisorError) as exc:
        print(f"CI_COPILOT_AUTOFIX_V2_PERSIST_BLOCKED={type(exc).__name__}:{str(exc)[:500]}", file=sys.stderr)
        return 2

    print("CI_COPILOT_AUTOFIX_V2_PERSISTED=YES")
    print(f"CI_COPILOT_AUTOFIX_V2_COMMIT_SHA={commit_sha}")
    print(f"CI_COPILOT_AUTOFIX_V2_COMMENT_ID={comment_id}")
    print("CI_COPILOT_AUTOFIX_V2_MERGE=DISABLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
