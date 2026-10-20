#!/usr/bin/env python3
"""Trusted default-branch path policy runner for GitHub Actions (no test execution)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib import request, error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops/provisioning/github-app-control-plane"))
from ci_impact_policy import (REPO, SUITES, Impact, classify_compare, fail_closed, get_pr_impact)  # noqa: E402


def github_request(path: str) -> tuple[int, object]:
    token = os.environ["GITHUB_TOKEN"]
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    req = request.Request(base + path, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "attention-router-ci-impact-v1",
    })
    with request.urlopen(req, timeout=20) as resp:
        return resp.status, json.load(resp)


def impact_for_event(event_name: str, event: dict, repo: str, actual_sha: str,
                     api=github_request) -> Impact:
    if repo != REPO or not isinstance(event, dict):
        return fail_closed("unrecognized_repository_or_event")
    if event_name == "pull_request":
        pr = event.get("pull_request") or {}
        sha = (pr.get("head") or {}).get("sha")
        number = event.get("number")
        if (not isinstance(sha, str) or not isinstance(number, int)
                or (pr.get("base") or {}).get("ref") != "main"):
            return fail_closed("untrusted_pr_event")
        return get_pr_impact(repo, number, sha, api)
    if event_name == "push" and event.get("ref") == "refs/heads/main":
        before, after = event.get("before"), event.get("after")
        if not isinstance(before, str) or not isinstance(after, str) or after != actual_sha:
            return fail_closed("push_head_mismatch")
        try:
            status, result = api(f"/repos/{repo}/compare/{before}...{after}?per_page=100")
            return classify_compare(before, after, result) if status == 200 else fail_closed("compare_api_failure")
        except (OSError, ValueError, KeyError, error.HTTPError):
            return fail_closed("compare_fetch_exception")
    return fail_closed("unsupported_workflow_event")


def main() -> None:
    # Never fail the classifier job with an unreported action status: fail CLOSED
    # to all expensive tests if the API, metadata or event is unavailable.
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        report = impact_for_event(
            os.environ.get("GITHUB_EVENT_NAME", ""), event,
            os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_SHA", ""),
        )
    except Exception:
        report = fail_closed("unavailable_event_or_env")
    output = Path(os.environ["GITHUB_OUTPUT"])
    with output.open("a") as stream:
        for suite in SUITES:
            stream.write(f"{suite}={'true' if report.required(suite) else 'false'}\n")
        stream.write(f"reason={report.reason}\n")
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    lines = ["### CI impact selection (trusted policy)", "",
             f"Policy: `{report.policy}` · Reason: `{report.reason}`",
             f"Changed paths: `{report.file_count}` · Paths digest: `{report.paths_sha256}`", "",
             "| Suite | Decision |", "|---|---|"]
    lines += [f"| `{suite}` | **{'RUN' if report.required(suite) else 'NOT APPLICABLE — no test run'}** |"
              for suite in SUITES]
    lines.append("\nAny unrecognized change or missing evidence requires the full suite. Secret scan and CodeQL are not exempted.\n")
    with summary.open("a") as stream:
        stream.write("\n".join(lines))
    print(f"CI_IMPACT_POLICY={report.policy} REASON={report.reason} "
          f"FILES={report.file_count} SUITES={','.join(sorted(report.suites)) or 'none'}")


if __name__ == "__main__":
    main()
