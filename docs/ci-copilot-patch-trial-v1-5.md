# CI Copilot Patch Trial V1.5

## Purpose

V1.5 extends the proven read-only Copilot Advisor with a strictly **ephemeral patch trial**. The model may propose a small unified diff, but the diff is applied only to an exact-head disposable checkout inside the GitHub Actions runner. The patch is never committed, pushed or merged.

The experimental path is:

`failed Public CI -> V0 deterministic triage -> V1 Copilot diagnosis -> explicit proof-branch gate -> exact PR-head disposable checkout -> minimized failure evidence + bounded test-file contents -> Copilot patch proposal -> deterministic patch guardian -> git apply in runner only -> targeted validation -> hard reset/clean -> sticky trial comment`

## Explicit opt-in

V1.5 is not enabled for ordinary pull requests. The patch-trial job runs only when the triggering branch starts with:

`ci-agent-patch-trial/`

This branch-prefix gate is an experimental proving mechanism, not the final user-facing opt-in design.

## Patchable scope

V1.5 is intentionally narrower than V1. The patch trial is limited to:

- `TEST_FAILURE`
- `LINT_FAILURE`
- `COMPILE_FAILURE`

and may modify only existing `tests/*.py` files that are already changed by the current pull request.

The model cannot patch application source, `.github/`, workflows, deployment, migrations, authority, policy, security, infrastructure, configuration or any new/deleted/renamed file in V1.5.

## Model input

The patch model receives only:

- the V1 sanitized failure packet;
- bounded contents of up to three patchable changed test files;
- deterministic patch budgets and authority constraints.

All filenames, source text and log excerpts remain untrusted data, never instructions. Copilot still receives no shell/read/write/URL/memory tools.

## Patch contract

The model must return exactly one `attention-router-ci-copilot-patch.v1.5` JSON object with:

- `decision`: `PATCH`, `NEEDS_HUMAN` or `INSUFFICIENT_CONTEXT`;
- `confidence`: `LOW`, `MEDIUM` or `HIGH`;
- short summary;
- target files;
- unified diff string.

A `PATCH` decision requires `HIGH` confidence.

The guardian rejects patches that:

- touch a file outside the changed `tests/*.py` allowlist;
- touch more than two files;
- exceed 60 changed lines;
- exceed 12,000 patch characters;
- create/delete/rename files;
- change file modes;
- contain binary patches;
- have mismatched unified-diff paths;
- fail `git apply --check` or `git diff --check`.

## Disposable validation

After a patch passes the guardian, it is applied only in the `candidate/` checkout at the exact PR head. Validation runs with GitHub/Copilot/OIDC credentials removed from the subprocess environment.

V1.5 runs deterministic targeted validation only:

- `ruff` against patched test files;
- `compileall` against patched test files;
- `pytest` against test paths found in the sanitized failure excerpt, falling back to the patched test files.

The job then always executes `git reset --hard HEAD` and `git clean -fdx` and verifies a clean workspace before publishing the result.

## Repository authority

The workflow remains:

- `actions: read`
- `contents: read`
- `pull-requests: write`
- `copilot-requests: write` only in Copilot jobs

There is still no `contents: write`, no persisted Git credential, no commit command, no push command and no merge path.

## Evidence

The sticky `Copilot Patch Trial V1.5` comment records only bounded evidence:

- exact PR head;
- trial status;
- patched test filenames;
- changed-line budget consumed;
- SHA-256 of the ephemeral patch;
- deterministic validation commands;
- cleanup state.

The actual generated patch is not posted to the PR by V1.5.

## Promotion boundary

A later V2 may be considered only after V1.5 proves on a controlled failed-CI event that a model-generated patch can be constrained, applied, validated and fully discarded. V1.5 itself grants no persistent repository mutation authority.
