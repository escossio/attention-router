# GitHub CI Agent V0

## Purpose

CI Agent V0 turns GitHub Actions completion into an event-driven, fail-closed triage loop for pull requests. It removes the need for a human to repeatedly tell an external assistant that a workflow finished or failed.

The V0 is intentionally observation-only with respect to repository contents. It proves event delivery, exact-head correlation, deterministic classification and a single sticky PR status surface before any code mutation authority is introduced.

## Event path

`Public CI / CodeQL completed -> workflow_run -> trusted default-branch CI Agent -> exact-head verification -> workflow/job inspection -> sticky PR comment`

The `workflow_run` handler is active only after `.github/workflows/ci-agent.yml` exists on the repository default branch.

## Security boundary

The workflow has only:

- `actions: read`
- `contents: read`
- `pull-requests: write`

`pull-requests: write` exists only because the GitHub REST issue-comment endpoint used for pull-request conversation comments accepts Pull requests write as an authorization path. V0 still has no repository-content write permission, no workflow mutation permission and no merge authority.

The checkout always reads the trusted default branch rather than the pull-request head. No repository or provider secret is consumed; the only credential is the ephemeral GitHub Actions token.

Fork-origin workflow runs are rejected. Events whose completed run head is no longer the current PR head are rejected as stale before job triage or comment mutation. Dependabot-origin runs are observation-only at the upstream CI layer and are skipped by this V0 comment writer because GitHub can restrict write-token behavior for bot-originated pull requests.

## Real-event finding

The first live proof after V0 activation showed that `workflow_run` delivery and exact-head triage worked, but `issues: write` + `pull-requests: read` received `403 Resource not accessible by integration` when creating the sticky comment on a normal repository PR. The V0.5 correction changes the comment authorization path to `pull-requests: write` while retaining `contents: read`.

This is an integration-permission correction, not an expansion into code mutation. The agent still cannot commit, push or merge.

## State model

The agent reconstructs the current exact-head state of `Public CI` and `CodeQL` and reports one of:

- `GREEN`: both required workflows completed successfully.
- `FAILED`: at least one required workflow concluded with failure.
- `WAITING`: a required workflow exists but is still running.
- `BLOCKED`: a required workflow was cancelled, timed out or otherwise blocked.
- `INCOMPLETE`: one of the required workflow observations is missing or inconclusive.

Failed job steps are mapped to bounded technical categories such as `TEST_FAILURE`, `LINT_FAILURE`, `COMPILE_FAILURE`, `DOCKER_BUILD_FAILURE`, `TRANSPORT_TEST_FAILURE`, `SECURITY_ANALYSIS_FAILURE` or `AMBIGUOUS_FAILURE`.

## PR surface

The agent owns one sticky comment identified by `<!-- attention-router-ci-agent:v0 -->`. Subsequent workflow completions update that comment rather than creating a new comment for every event.

The comment contains only workflow/job/step status and technical classification. It does not copy raw logs, payloads, credentials, conversation data or environment inventory.

## Autofix boundary

`AUTOFIX=DISABLED` is structural in V0. Every classification has `auto_fix_eligible=False`, and the workflow token cannot write repository contents.

A future V1 may introduce constrained fixes only after V0 is proven on real PRs. The minimum V1 controls are:

1. same-repository PR only;
2. current-head verification immediately before mutation;
3. allowlisted failure classes only;
4. one bounded correction attempt per head/failure fingerprint;
5. maximum file/change budget;
6. no workflow, security-policy, branch-protection, secret, deployment or authority-boundary changes;
7. no merge authority;
8. a new CI round must validate every agent commit;
9. ambiguous or security-sensitive failures escalate to a human instead of mutating code.

## Chat integration

The event-driven runtime lives in GitHub, not inside one interactive chat process. The PR sticky comment becomes the durable event surface that ChatGPT and other authorized tools can inspect. A later notification bridge can surface those state changes into chat without changing the CI Agent's repository authority.

This separation keeps CI reaction real-time while preserving the chat as the human control plane rather than making a chat session itself a long-running server.
