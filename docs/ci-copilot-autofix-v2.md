# CI Copilot Autofix V2

## Purpose

V2 is the first CI Agent boundary that may persist a code correction to a pull-request branch. It does **not** grant write authority to Copilot itself and it never merges a pull request.

The event path is:

`Public CI failure -> V0 triage -> V1 Advisor gate -> read-only V2 proposal -> ephemeral validation -> deterministic V2 persistence revalidation -> one bounded commit -> normal CI reruns`

## Explicit opt-in

Persistent autofix runs only for same-repository pull requests whose branch starts with:

`ci-agent-autofix/`

Ordinary feature branches, Dependabot branches, forks, CodeQL/security failures and non-allowlisted failure categories never enter V2 persistence.

## Split authority

V2 deliberately uses two different jobs.

### `autofix-proposal`

This job may call Copilot CLI, but it has only:

- `actions: read`
- `contents: read`
- `pull-requests: write`
- `copilot-requests: write`

It receives the same minimized/sanitized failure evidence used by V1, plus bounded contents of already-changed `tests/*.py` files. It cannot persist repository contents.

The proposal must pass the existing V1 Advisor gate on the exact same head:

- `PROPOSE_FIX`
- confidence `HIGH`
- risk `NONE`

The generated patch then passes the V1.5 contract and ephemeral validation before it can become an input to the persistence job.

### `autofix-persist`

This job does **not** install or invoke Copilot and has no `copilot-requests` permission. It receives only the already-validated proposal from the previous job and has:

- `actions: read`
- `contents: write`
- `pull-requests: write`

Both checkouts use `persist-credentials: false`. Candidate tests run with GitHub, Copilot and OIDC credentials stripped from the subprocess environment.

## Scope

V2 initially preserves the exact narrow V1.5 patch scope:

- existing files only;
- already changed by the pull request;
- Python files under `tests/` only;
- `TEST_FAILURE`, `LINT_FAILURE` or `COMPILE_FAILURE` only;
- maximum 2 files;
- maximum 60 changed lines;
- maximum 12k patch characters;
- no binary patch, create, delete, rename or mode change.

Source code, workflows, deployment, migrations, security, authority/policy code, configuration and transport runtime remain outside persistent autofix V2.

## Persistence protocol

The persistence job performs the following sequence:

1. verify the PR is still open, same-repository, same branch and exact source head;
2. verify the one-attempt budget has not been consumed;
3. decode and validate the proposal artifact and SHA-256;
4. re-run deterministic ephemeral validation from a credential-free exact-head checkout;
5. materialize the same patch again only to capture the final bounded file contents;
6. reset/clean the candidate workspace and require it to be clean;
7. verify the live PR head again immediately before persistence;
8. create blobs, one tree and one commit through the GitHub Git Data API;
9. update the PR branch ref with `force: false`;
10. record the source head, patch SHA-256 and resulting agent commit in one sticky V2 comment.

No `git push` or shell `git commit` path is used.

## Attempt and loop budget

V2 permits **one persistent autofix commit per PR**. Once the sticky V2 comment records `PERSISTED`, all later V2 write attempts on that PR are blocked, including failures on the agent-created commit.

This is deliberately conservative. A later version may define a human-resettable budget, but V2 does not infer that a new push should automatically restore write authority.

## Concurrency

CI Agent concurrency remains grouped by PR, but `cancel-in-progress` is disabled. A new workflow completion queues behind an active agent run instead of cancelling a proposal/persistence sequence halfway through. Exact-head checks make queued stale runs fail closed.

## Commit identity

The single generated commit uses the subject:

`ci-agent: apply validated autofix`

and records trailers for:

- V2 version;
- exact source head;
- patch SHA-256;
- merge disabled.

## Merge authority

Automated merge remains prohibited. After the V2 commit, normal Public CI and CodeQL must validate the new head. A green result is evidence only; it does not authorize merge.

## Proof plan

After this implementation PR is green and merged:

1. create a controlled `ci-agent-autofix/...` branch;
2. introduce one synthetic pytest failure in an existing test file;
3. require V0 and V1 to establish the safe diagnosis gate;
4. require the proposal job to pass ephemeral validation without repository write authority;
5. require the persistence job to revalidate and create exactly one non-force commit;
6. require the agent-created head to pass normal Public CI and CodeQL;
7. require a second V2 write attempt to be blocked by the one-attempt budget;
8. close the controlled proof PR without merging it.

Only after that proof should persistent autofix be considered technically demonstrated. Broader file scope and automated merge remain separate future boundaries.
