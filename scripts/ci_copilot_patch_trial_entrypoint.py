from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from scripts import ci_agent
from scripts import ci_copilot_advisor as advisor
from scripts import ci_copilot_patch_trial as trial


def advisor_gate_reason(repo: str, pr_number: int, head_sha: str, *, token: str) -> str | None:
    comments = ci_agent._request_json(
        "GET",
        ci_agent._api_url(repo, f"issues/{pr_number}/comments", per_page="100"),
        token=token,
    )
    candidates = [
        comment
        for comment in comments
        if advisor.ADVISOR_MARKER in str(comment.get("body") or "")
        and str((comment.get("user") or {}).get("type") or "") == "Bot"
    ]
    if not candidates:
        return "ADVISOR_EVIDENCE_MISSING"

    body = str(candidates[-1].get("body") or "")
    if f"head: `{head_sha[:12]}`" not in body:
        return "ADVISOR_EVIDENCE_STALE_HEAD"
    if "DRY-RUN · PROPOSE_FIX" not in body:
        return "ADVISOR_NOT_PROPOSE_FIX"
    if "**Confiança:** `HIGH`" not in body:
        return "ADVISOR_CONFIDENCE_NOT_HIGH"
    if "**Risk flags:** `NONE`" not in body:
        return "ADVISOR_RISK_NOT_NONE"
    return None


def main(argv: list[str] | None = None) -> int:
    args = trial._parse_args(argv or sys.argv[1:])
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    reason = advisor_gate_reason(args.repo, args.pr_number, args.head_sha, token=token)
    if reason is None:
        return trial.main(argv)

    body = trial.render_patch_trial_comment(
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        generated_at=datetime.now(UTC),
        blocked_reason=reason,
    )
    comment_id = trial._upsert_comment(args.repo, args.pr_number, body, token=token)
    print(f"CI_COPILOT_PATCH_TRIAL_COMMENT_ID={comment_id}")
    print("CI_COPILOT_PATCH_TRIAL_MODEL_CALLED=NO")
    print("CI_COPILOT_PATCH_TRIAL_PERSISTENT_MUTATION=NO")
    print(f"CI_COPILOT_PATCH_TRIAL_GATE={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
