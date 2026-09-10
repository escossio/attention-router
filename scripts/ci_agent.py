from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable


AGENT_MARKER = "<!-- attention-router-ci-agent:v0 -->"
TARGET_WORKFLOWS = ("Public CI", "CodeQL")


@dataclass(frozen=True, slots=True)
class FailedStep:
    workflow: str
    job: str
    step: str
    category: str
    auto_fix_eligible: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowState:
    name: str
    run_id: int | None
    status: str
    conclusion: str | None
    html_url: str | None
    failed_steps: tuple[FailedStep, ...] = ()


class GitHubApiError(RuntimeError):
    pass


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "attention-router-ci-agent-v0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(f"GitHub API {method} {url} failed: {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubApiError(f"GitHub API {method} {url} failed: {exc.reason}") from exc
    return json.loads(body) if body else None


def _api_url(repo: str, path: str, **query: str) -> str:
    base = f"https://api.github.com/repos/{repo}/{path.lstrip('/')}"
    if not query:
        return base
    return f"{base}?{urllib.parse.urlencode(query)}"


def classify_failed_step(workflow: str, job: str, step: str) -> FailedStep:
    normalized = f"{job} {step}".lower()
    if workflow == "CodeQL":
        category = "SECURITY_ANALYSIS_FAILURE"
    elif "pytest" in normalized or "postgresql integration tests" in normalized:
        category = "TEST_FAILURE"
    elif "ruff" in normalized:
        category = "LINT_FAILURE"
    elif "compileall" in normalized:
        category = "COMPILE_FAILURE"
    elif "docker build" in normalized:
        category = "DOCKER_BUILD_FAILURE"
    elif "npm test" in normalized or "transport" in normalized:
        category = "TRANSPORT_TEST_FAILURE"
    else:
        category = "AMBIGUOUS_FAILURE"
    # V0 is observation-only by construction. No category may mutate code.
    return FailedStep(
        workflow=workflow,
        job=job,
        step=step,
        category=category,
        auto_fix_eligible=False,
    )


def failed_steps_from_jobs(workflow: str, jobs: Iterable[dict[str, Any]]) -> tuple[FailedStep, ...]:
    failed: list[FailedStep] = []
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        steps = list(job.get("steps") or [])
        failed_job_steps = [step for step in steps if step.get("conclusion") == "failure"]
        if not failed_job_steps:
            failed.append(classify_failed_step(workflow, str(job.get("name") or "unknown"), "unknown"))
            continue
        for step in failed_job_steps:
            failed.append(
                classify_failed_step(
                    workflow,
                    str(job.get("name") or "unknown"),
                    str(step.get("name") or "unknown"),
                )
            )
    return tuple(failed)


def select_latest_runs(
    runs: Iterable[dict[str, Any]],
    *,
    target_workflows: Iterable[str] = TARGET_WORKFLOWS,
) -> dict[str, dict[str, Any]]:
    targets = set(target_workflows)
    selected: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        if name not in targets:
            continue
        current = selected.get(name)
        candidate_key = (str(run.get("created_at") or ""), int(run.get("id") or 0))
        current_key = (
            str(current.get("created_at") or ""),
            int(current.get("id") or 0),
        ) if current is not None else ("", 0)
        if current is None or candidate_key > current_key:
            selected[name] = run
    return selected


def derive_overall_state(states: Iterable[WorkflowState]) -> str:
    values = tuple(states)
    if not values or any(state.run_id is None for state in values):
        return "INCOMPLETE"
    if any(state.status != "completed" for state in values):
        return "WAITING"
    conclusions = {state.conclusion for state in values}
    if conclusions == {"success"}:
        return "GREEN"
    if "failure" in conclusions:
        return "FAILED"
    if conclusions & {"cancelled", "timed_out", "action_required", "startup_failure"}:
        return "BLOCKED"
    return "INCOMPLETE"


def render_comment(
    *,
    pr_number: int,
    head_sha: str,
    states: Iterable[WorkflowState],
    generated_at: datetime,
) -> str:
    states = tuple(states)
    overall = derive_overall_state(states)
    symbol = {
        "GREEN": "✅",
        "FAILED": "❌",
        "WAITING": "⏳",
        "BLOCKED": "🛑",
        "INCOMPLETE": "⚠️",
    }[overall]
    lines = [
        AGENT_MARKER,
        f"### CI Agent V0 {symbol} {overall}",
        "",
        f"PR: `#{pr_number}` · head: `{head_sha[:12]}` · updated: `{generated_at.astimezone(UTC).isoformat()}`",
        "",
        "| Workflow | Status | Conclusion |",
        "| --- | --- | --- |",
    ]
    for state in states:
        status = state.status
        conclusion = state.conclusion or "—"
        if state.html_url:
            workflow_label = f"[{state.name}]({state.html_url})"
        else:
            workflow_label = state.name
        lines.append(f"| {workflow_label} | `{status}` | `{conclusion}` |")

    failures = tuple(step for state in states for step in state.failed_steps)
    if failures:
        lines.extend(["", "**Falhas observadas**"])
        for failure in failures:
            lines.append(
                f"- `{failure.workflow}` → `{failure.job}` → `{failure.step}` "
                f"→ **{failure.category}**"
            )

    lines.extend(
        [
            "",
            "**Autoridade do agente V0:** observação e triagem apenas. `AUTOFIX=DISABLED`. ",
            "Ele não possui `contents: write`, não pode fazer commit e nunca faz merge.",
        ]
    )
    if overall == "GREEN":
        lines.append("\nPróxima ação: fronteira de CI está verde; decisão de avançar/mergear continua humana.")
    elif overall == "FAILED":
        lines.append("\nPróxima ação: falha classificada; correção automática permanece bloqueada no V0.")
    elif overall == "WAITING":
        lines.append("\nPróxima ação: aguardar os workflows restantes.")
    else:
        lines.append("\nPróxima ação: revisão humana necessária antes de qualquer mutação.")
    return "\n".join(lines).rstrip() + "\n"


def _fetch_runs_for_head(repo: str, head_sha: str, *, token: str) -> list[dict[str, Any]]:
    payload = _request_json(
        "GET",
        _api_url(repo, "actions/runs", head_sha=head_sha, event="pull_request", per_page="100"),
        token=token,
    )
    return list(payload.get("workflow_runs") or [])


def _fetch_jobs(repo: str, run_id: int, *, token: str) -> list[dict[str, Any]]:
    payload = _request_json(
        "GET",
        _api_url(repo, f"actions/runs/{run_id}/jobs", per_page="100"),
        token=token,
    )
    return list(payload.get("jobs") or [])


def build_workflow_states(repo: str, head_sha: str, *, token: str) -> tuple[WorkflowState, ...]:
    selected = select_latest_runs(_fetch_runs_for_head(repo, head_sha, token=token))
    states: list[WorkflowState] = []
    for name in TARGET_WORKFLOWS:
        run = selected.get(name)
        if run is None:
            states.append(
                WorkflowState(
                    name=name,
                    run_id=None,
                    status="missing",
                    conclusion=None,
                    html_url=None,
                )
            )
            continue
        run_id = int(run["id"])
        jobs = _fetch_jobs(repo, run_id, token=token) if run.get("status") == "completed" else []
        states.append(
            WorkflowState(
                name=name,
                run_id=run_id,
                status=str(run.get("status") or "unknown"),
                conclusion=run.get("conclusion"),
                html_url=run.get("html_url"),
                failed_steps=failed_steps_from_jobs(name, jobs),
            )
        )
    return tuple(states)


def _upsert_comment(repo: str, pr_number: int, body: str, *, token: str) -> int:
    comments = _request_json(
        "GET",
        _api_url(repo, f"issues/{pr_number}/comments", per_page="100"),
        token=token,
    )
    existing = next(
        (
            comment
            for comment in comments
            if AGENT_MARKER in str(comment.get("body") or "")
            and str((comment.get("user") or {}).get("type") or "") == "Bot"
        ),
        None,
    )
    if existing is None:
        created = _request_json(
            "POST",
            _api_url(repo, f"issues/{pr_number}/comments"),
            token=token,
            payload={"body": body},
        )
        return int(created["id"])
    comment_id = int(existing["id"])
    _request_json(
        "PATCH",
        _api_url(repo, f"issues/comments/{comment_id}"),
        token=token,
        payload={"body": body},
    )
    return comment_id


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Event-driven GitHub CI triage agent V0")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    states = build_workflow_states(args.repo, args.head_sha, token=token)
    body = render_comment(
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        states=states,
        generated_at=datetime.now(UTC),
    )
    if args.dry_run:
        print(body)
        return 0
    comment_id = _upsert_comment(args.repo, args.pr_number, body, token=token)
    print(f"CI_AGENT_COMMENT_ID={comment_id}")
    print(f"CI_AGENT_OVERALL={derive_overall_state(states)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
