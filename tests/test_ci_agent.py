from datetime import UTC, datetime
from pathlib import Path

from scripts import ci_agent


WORKFLOW = Path(".github/workflows/ci-agent.yml")


def _state(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    failed_steps: tuple[ci_agent.FailedStep, ...] = (),
) -> ci_agent.WorkflowState:
    return ci_agent.WorkflowState(
        name=name,
        run_id=1,
        status=status,
        conclusion=conclusion,
        html_url=f"https://example.test/{name}",
        failed_steps=failed_steps,
    )


def test_failed_step_classification_is_observation_only():
    pytest_failure = ci_agent.classify_failed_step(
        "Public CI",
        "python-tests",
        "Run python -m pytest -q",
    )
    codeql_failure = ci_agent.classify_failed_step(
        "CodeQL",
        "analyze (python)",
        "Analyze",
    )

    assert pytest_failure.category == "TEST_FAILURE"
    assert codeql_failure.category == "SECURITY_ANALYSIS_FAILURE"
    assert pytest_failure.auto_fix_eligible is False
    assert codeql_failure.auto_fix_eligible is False


def test_failed_steps_are_derived_only_from_failed_jobs_and_steps():
    jobs = [
        {
            "name": "python-tests",
            "conclusion": "failure",
            "steps": [
                {"name": "ruff", "conclusion": "success"},
                {"name": "Run python -m pytest -q", "conclusion": "failure"},
            ],
        },
        {
            "name": "docker-build",
            "conclusion": "success",
            "steps": [{"name": "docker build", "conclusion": "success"}],
        },
    ]

    failures = ci_agent.failed_steps_from_jobs("Public CI", jobs)

    assert len(failures) == 1
    assert failures[0].job == "python-tests"
    assert failures[0].category == "TEST_FAILURE"


def test_latest_run_selection_is_per_workflow():
    runs = [
        {"id": 1, "name": "Public CI", "created_at": "2026-09-10T01:00:00Z"},
        {"id": 2, "name": "Public CI", "created_at": "2026-09-10T02:00:00Z"},
        {"id": 3, "name": "CodeQL", "created_at": "2026-09-10T01:30:00Z"},
        {"id": 4, "name": "Other", "created_at": "2026-09-10T03:00:00Z"},
    ]

    selected = ci_agent.select_latest_runs(runs)

    assert selected["Public CI"]["id"] == 2
    assert selected["CodeQL"]["id"] == 3
    assert "Other" not in selected


def test_overall_state_waits_fails_and_goes_green_deterministically():
    assert ci_agent.derive_overall_state(
        (_state("Public CI"), _state("CodeQL", status="in_progress", conclusion=None))
    ) == "WAITING"
    assert ci_agent.derive_overall_state(
        (_state("Public CI", conclusion="failure"), _state("CodeQL"))
    ) == "FAILED"
    assert ci_agent.derive_overall_state((_state("Public CI"), _state("CodeQL"))) == "GREEN"


def test_comment_is_sticky_minimized_and_explicitly_non_mutating():
    failure = ci_agent.classify_failed_step(
        "Public CI",
        "python-tests",
        "Run python -m pytest -q",
    )
    body = ci_agent.render_comment(
        pr_number=17,
        head_sha="a" * 40,
        states=(
            _state("Public CI", conclusion="failure", failed_steps=(failure,)),
            _state("CodeQL"),
        ),
        generated_at=datetime(2026, 9, 10, 2, 30, tzinfo=UTC),
    )

    assert ci_agent.AGENT_MARKER in body
    assert "CI Agent V0" in body
    assert "TEST_FAILURE" in body
    assert "AUTOFIX=DISABLED" in body
    assert "contents: write" in body
    assert "nunca faz merge" in body


def test_existing_bot_marker_is_patched_instead_of_posting_second_comment(monkeypatch):
    calls = []

    def fake_request(method, url, *, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return [
                {
                    "id": 456,
                    "body": f"{ci_agent.AGENT_MARKER}\nold state",
                    "user": {"type": "Bot"},
                }
            ]
        if method == "PATCH":
            return {"id": 456, "body": payload["body"]}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(ci_agent, "_request_json", fake_request)

    comment_id = ci_agent._upsert_comment(
        "escossio/attention-router",
        19,
        f"{ci_agent.AGENT_MARKER}\nnew state",
        token="synthetic-token",
    )

    assert comment_id == 456
    assert [method for method, _, _ in calls] == ["GET", "PATCH"]
    assert "/issues/comments/456" in calls[-1][1]


def test_stale_workflow_event_exits_before_triage_or_comment(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token")
    monkeypatch.setattr(ci_agent, "_fetch_current_pr_head", lambda *args, **kwargs: "b" * 40)

    called = {"triage": False, "comment": False}

    def unexpected_triage(*args, **kwargs):
        called["triage"] = True
        raise AssertionError("stale event must not triage")

    def unexpected_comment(*args, **kwargs):
        called["comment"] = True
        raise AssertionError("stale event must not comment")

    monkeypatch.setattr(ci_agent, "build_workflow_states", unexpected_triage)
    monkeypatch.setattr(ci_agent, "_upsert_comment", unexpected_comment)

    result = ci_agent.main(
        [
            "--repo",
            "escossio/attention-router",
            "--pr-number",
            "17",
            "--head-sha",
            "a" * 40,
        ]
    )

    assert result == 0
    assert called == {"triage": False, "comment": False}
    assert "CI_AGENT_STALE_HEAD=YES" in capsys.readouterr().out


def test_current_head_can_update_the_single_agent_comment(monkeypatch):
    head = "c" * 40
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token")
    monkeypatch.setattr(ci_agent, "_fetch_current_pr_head", lambda *args, **kwargs: head)
    monkeypatch.setattr(
        ci_agent,
        "build_workflow_states",
        lambda *args, **kwargs: (_state("Public CI"), _state("CodeQL")),
    )
    observed = {}

    def fake_upsert(repo, pr_number, body, *, token):
        observed.update(repo=repo, pr_number=pr_number, body=body, token=token)
        return 123

    monkeypatch.setattr(ci_agent, "_upsert_comment", fake_upsert)

    result = ci_agent.main(
        [
            "--repo",
            "escossio/attention-router",
            "--pr-number",
            "17",
            "--head-sha",
            head,
        ]
    )

    assert result == 0
    assert observed["pr_number"] == 17
    assert observed["token"] == "synthetic-token"
    assert "GREEN" in observed["body"]


def test_workflow_run_boundary_allows_only_pr_comment_write_and_trusted_code():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    triage_job = workflow.split("\n  triage:\n", 1)[1].split("\n  copilot-advisor:\n", 1)[0]

    assert 'workflows: ["Public CI", "CodeQL"]' in workflow
    assert "types: [completed]" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in triage_job
    assert "github.event.workflow_run.actor.login != 'dependabot[bot]'" in triage_job
    assert "ref: ${{ github.event.repository.default_branch }}" in triage_job
    assert "persist-credentials: false" in triage_job
    assert "contents: write" not in triage_job
    assert "issues: write" not in triage_job
    assert workflow.count("contents: write") == 1
    assert "autofix-persist:" in workflow
    assert "secrets." not in workflow
    assert "github.event.workflow_run.head_sha" in workflow
