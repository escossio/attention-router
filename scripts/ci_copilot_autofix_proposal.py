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
from scripts import ci_copilot_patch_trial as trial
from scripts.ci_copilot_advisor_entrypoint import safe_request_text
from scripts.ci_copilot_patch_trial_entrypoint import advisor_gate_reason


AUTOFIX_MARKER = "<!-- attention-router-ci-copilot-autofix:v2 -->"
AUTOFIX_PROPOSAL_SCHEMA = "attention-router-ci-copilot-autofix-proposal.v2"
AUTOFIX_BRANCH_PREFIX = "ci-agent-autofix/"
MAX_PERSISTENT_ATTEMPTS = 1


class AutofixProposalError(RuntimeError):
    pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Copilot autofix proposal V2")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--trigger-workflow", required=True)
    parser.add_argument("--trigger-run-id", required=True, type=int)
    parser.add_argument("--candidate-dir", required=True)
    return parser.parse_args(argv)


def _comments(repo: str, pr_number: int, *, token: str) -> list[dict[str, Any]]:
    payload = ci_agent._request_json(
        "GET", ci_agent._api_url(repo, f"issues/{pr_number}/comments", per_page="100"), token=token
    )
    return list(payload)


def persistent_attempt_used(repo: str, pr_number: int, *, token: str) -> bool:
    return any(
        AUTOFIX_MARKER in str(comment.get("body") or "")
        and "**State:** `PERSISTED`" in str(comment.get("body") or "")
        for comment in _comments(repo, pr_number, token=token)
    )


def _upsert_autofix_comment(repo: str, pr_number: int, body: str, *, token: str) -> int:
    existing = next(
        (
            comment
            for comment in _comments(repo, pr_number, token=token)
            if AUTOFIX_MARKER in str(comment.get("body") or "")
            and str((comment.get("user") or {}).get("type") or "") == "Bot"
        ),
        None,
    )
    if existing is None:
        created = ci_agent._request_json(
            "POST",
            ci_agent._api_url(repo, f"issues/{pr_number}/comments"),
            token=token,
            payload={"body": body},
        )
        return int(created["id"])
    comment_id = int(existing["id"])
    ci_agent._request_json(
        "PATCH",
        ci_agent._api_url(repo, f"issues/comments/{comment_id}"),
        token=token,
        payload={"body": body},
    )
    return comment_id


def _render_comment(
    *,
    pr_number: int,
    head_sha: str,
    state: str,
    detail: str,
    patch_sha256: str | None = None,
    target_files: tuple[str, ...] = (),
) -> str:
    lines = [
        AUTOFIX_MARKER,
        f"### Copilot Autofix V2 · {state}",
        "",
        f"PR: `#{pr_number}` · source head: `{head_sha[:12]}` · updated: `{datetime.now(UTC).isoformat()}`",
        "",
        f"**State:** `{state}`",
        f"**Attempt budget:** `0/{MAX_PERSISTENT_ATTEMPTS}` persistent writes used",
        "**Authority split:** Copilot proposal job is repository read-only. Persistence is a separate deterministic job.",
        "**Merge:** `DISABLED`",
        "",
        f"**Detail:** {detail[:1000]}",
    ]
    if target_files:
        lines.extend(["", "**Validated targets:** " + ", ".join(f"`{item}`" for item in target_files)])
    if patch_sha256:
        lines.append(f"**Patch SHA-256:** `{patch_sha256}`")
    return "\n".join(lines).rstrip() + "\n"


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _emit_not_ready(reason: str) -> None:
    _write_output("ready", "false")
    print("CI_COPILOT_AUTOFIX_V2_READY=NO")
    print(f"CI_COPILOT_AUTOFIX_V2_BLOCK={reason}")


def _encode_artifact(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _candidate_head(candidate_dir: Path) -> str:
    return trial._run(["git", "rev-parse", "HEAD"], cwd=candidate_dir, timeout=20).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    advisor._request_text = safe_request_text
    current_head = ci_agent._fetch_current_pr_head(args.repo, args.pr_number, token=token)
    if current_head != args.head_sha:
        _emit_not_ready("STALE_HEAD")
        return 0

    run = advisor._fetch_trigger_run(args.repo, args.trigger_run_id, token=token)
    try:
        advisor._validate_trigger(
            run,
            workflow=args.trigger_workflow,
            head_sha=args.head_sha,
            pr_number=args.pr_number,
        )
    except advisor.AdvisorError as exc:
        print(f"CI_COPILOT_AUTOFIX_V2_INVALID_TRIGGER={exc}", file=sys.stderr)
        return 2

    branch = str(run.get("head_branch") or "")
    if not branch.startswith(AUTOFIX_BRANCH_PREFIX):
        _emit_not_ready("BRANCH_NOT_OPTED_IN")
        return 0
    if persistent_attempt_used(args.repo, args.pr_number, token=token):
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="BUDGET_EXHAUSTED",
            detail="V2 permits only one persistent autofix attempt per PR.",
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready("ATTEMPT_BUDGET_EXHAUSTED")
        return 0

    gate_reason = advisor_gate_reason(args.repo, args.pr_number, args.head_sha, token=token)
    if gate_reason is not None:
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="POLICY_BLOCKED",
            detail=f"Advisor V1 gate rejected persistence proposal: {gate_reason}.",
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready(gate_reason)
        return 0

    candidate_dir = Path(args.candidate_dir).resolve()
    if not candidate_dir.is_dir() or not (candidate_dir / ".git").exists():
        print("candidate-dir must be an exact-head git checkout", file=sys.stderr)
        return 2
    if _candidate_head(candidate_dir) != args.head_sha:
        print("CI_COPILOT_AUTOFIX_V2_CANDIDATE_HEAD_MISMATCH=YES", file=sys.stderr)
        return 2

    failures, log_excerpt = advisor._fetch_failure_context(
        args.repo, args.trigger_workflow, args.trigger_run_id, token=token
    )
    changed_files = advisor._fetch_changed_files(args.repo, args.pr_number, token=token)
    categories = {failure.category for failure in failures}
    patchable = trial.patchable_changed_files(changed_files)
    if not failures or not categories <= trial.PATCHABLE_CATEGORIES or not patchable:
        reason = "FAILURE_OR_FILE_SCOPE_NOT_ALLOWLISTED"
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="POLICY_BLOCKED",
            detail=reason,
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready(reason)
        return 0

    snapshots = trial.collect_candidate_context(candidate_dir, changed_files)
    if not snapshots:
        _emit_not_ready("PATCHABLE_FILE_CONTEXT_UNAVAILABLE")
        return 0

    packet = advisor.build_failure_packet(
        repo=args.repo,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        workflow=args.trigger_workflow,
        run_id=args.trigger_run_id,
        failures=failures,
        changed_files=changed_files,
        log_excerpt=log_excerpt,
    )
    try:
        raw = advisor.invoke_copilot(trial.build_patch_prompt(failure_packet=packet, candidate_files=snapshots))
        proposal = trial.validate_patch_payload(
            advisor._extract_json_object(raw),
            patchable_files=patchable,
        )
    except (advisor.AdvisorError, trial.PatchTrialError) as exc:
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="UNAVAILABLE",
            detail=f"{type(exc).__name__}: {str(exc)[:500]}",
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready("MODEL_OR_CONTRACT_UNAVAILABLE")
        return 0

    if proposal.decision != "PATCH":
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="NO_PATCH",
            detail=f"Model returned {proposal.decision}: {proposal.summary}",
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready(proposal.decision)
        return 0

    validation = trial.apply_and_validate_ephemerally(
        proposal,
        candidate_dir=candidate_dir,
        log_excerpt=log_excerpt,
    )
    if validation.status != "VALIDATED_PASS" or not validation.cleanup_clean:
        body = _render_comment(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            state="VALIDATION_FAILED",
            detail=validation.detail,
            patch_sha256=validation.patch_sha256,
            target_files=validation.target_files,
        )
        _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)
        _emit_not_ready("EPHEMERAL_VALIDATION_FAILED")
        return 0

    patch_sha = hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
    artifact = {
        "schema": AUTOFIX_PROPOSAL_SCHEMA,
        "repo": args.repo,
        "pr_number": args.pr_number,
        "source_head": args.head_sha,
        "branch": branch,
        "trigger_workflow": args.trigger_workflow,
        "trigger_run_id": args.trigger_run_id,
        "patch_sha256": patch_sha,
        "log_excerpt": log_excerpt,
        "patch_proposal": {
            "schema": trial.PATCH_SCHEMA,
            "decision": proposal.decision,
            "confidence": proposal.confidence,
            "summary": proposal.summary,
            "target_files": list(proposal.target_files),
            "patch": proposal.patch,
        },
    }
    encoded = _encode_artifact(artifact)
    body = _render_comment(
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        state="PROPOSAL_VALIDATED",
        detail="Read-only proposal passed ephemeral validation and is ready for deterministic persistence revalidation.",
        patch_sha256=patch_sha,
        target_files=proposal.target_files,
    )
    comment_id = _upsert_autofix_comment(args.repo, args.pr_number, body, token=token)

    _write_output("ready", "true")
    _write_output("proposal_b64", encoded)
    _write_output("source_head", args.head_sha)
    _write_output("branch", branch)
    print("CI_COPILOT_AUTOFIX_V2_READY=YES")
    print(f"CI_COPILOT_AUTOFIX_V2_COMMENT_ID={comment_id}")
    print(f"CI_COPILOT_AUTOFIX_V2_PATCH_SHA256={patch_sha}")
    print("CI_COPILOT_AUTOFIX_V2_PERSISTENT_MUTATION=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
