# CI Copilot Advisor V1

## Purpose

Copilot Advisor V1 adds an AI diagnosis layer behind the deterministic CI Agent. It is deliberately **advice-only**: the model may explain an allowlisted technical CI failure, but it cannot edit repository contents, execute tools, apply a patch, push a commit or merge a pull request.

The event path is:

`Public CI failure -> workflow_run -> deterministic V0 triage -> exact-head verification -> sanitized failure packet -> Copilot CLI -> strict JSON validation -> sticky advisor comment`

CodeQL/security failures and ambiguous failures stop before model invocation and remain human-only.

## Authentication and billing

The workflow uses GitHub Actions' short-lived `GITHUB_TOKEN` and grants `copilot-requests: write` only to the `copilot-advisor` job. No personal access token or repository secret is introduced.

The Copilot CLI is pinned to `@github/copilot@1.0.83` and runs on Node.js 22.

## Input minimization

The advisor receives only a bounded failure packet containing:

- repository name;
- pull request number and exact head SHA;
- triggering workflow and run ID;
- failed job, failed step and deterministic failure category;
- up to 50 changed filenames;
- a bounded, signal-only log excerpt after deterministic redaction.

The advisor does **not** receive the pull-request body, raw GitHub event payload, full job log, environment inventory, provider credentials, repository secrets, private infrastructure details, personal data or conversation content.

Redaction removes token-shaped values, bearer credentials, email addresses, private IPv4 addresses, long numeric identifiers and whole lines that look like secret/token/password assignments.

## Prompt-injection boundary

Everything inside the failure packet is explicitly classified as untrusted data, never instructions. The CLI starts with custom repository instructions disabled and no interactive questions. The invocation denies shell, write, read, URL and memory tools.

The advisor must return one JSON object matching `attention-router-ci-copilot-advice.v1`. The deterministic validator rejects:

- unknown fields;
- invalid decisions/confidence/risk flags;
- suspected files not present in the current PR;
- oversized or empty recommendations;
- a `PROPOSE_FIX` result carrying elevated risk flags.

Invalid model output becomes `UNAVAILABLE`; it never becomes repository authority.

## Eligible failures

The V1 advisor may be invoked only for:

- `TEST_FAILURE`
- `LINT_FAILURE`
- `COMPILE_FAILURE`
- `DOCKER_BUILD_FAILURE`
- `TRANSPORT_TEST_FAILURE`

The following stop before Copilot invocation:

- `SECURITY_ANALYSIS_FAILURE`
- `AMBIGUOUS_FAILURE`
- any category outside the allowlist

## Output

The model can return only:

- `PROPOSE_FIX`
- `NEEDS_HUMAN`
- `INSUFFICIENT_CONTEXT`

along with confidence, a short diagnosis, suspected files already present in the PR, bounded recommended actions and risk flags.

The result is rendered in one sticky `Copilot Advisor V1` PR comment. V1 never includes a generated patch.

## Repository authority

The workflow keeps:

- `actions: read`
- `contents: read`
- `pull-requests: write`

and grants only the advisor job:

- `copilot-requests: write`

There is no `contents: write`, no Git credential persistence, no merge permission, no `--yolo`, no allow-all tool mode and no repository/provider secret.

## Proven boundary

V1 has been proven on a controlled failed-CI event. The agent classified a synthetic pytest failure, collected and sanitized the failing-job log through the hardened redirect reader, invoked Copilot, validated the model response and published a `PROPOSE_FIX` diagnosis with no repository write authority.

## Next boundary

V1.5 may let Copilot produce an **ephemeral patch inside a disposable runner workspace** only after deterministic policy checks. The patch must remain non-persistent: no commit, push or merge. The guardian must enforce explicit opt-in, exact-head matching, file/path allowlists, patch budgets, deterministic test commands and explicit cleanup after validation.
