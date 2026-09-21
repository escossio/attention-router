# Project status

Updated: 2026-09-21

## Andy Ops Live Supervisor V1

- Added a sanitized, reproducible LAN-only operational panel package under `ops/provisioning/andy-ops-panel/`.
- Compute view shows AGT/CI01/CI02/CI03 CPU total + per-core btop-like meters, CPU temperature when exposed, current CI task, elapsed time and recent distributed dispatches.
- Chat/Console view observes the concrete Chat -> Remote Desktop Commander -> AGT channel using the plugin's persistent JSONL tool history and active console descendants; it does not claim to observe ChatGPT UI internals.
- V1 is read-only, has no production database access, and public files contain no private LAN address or credentials.
- Runtime proof completed on the control plane: systemd active, health PASS, live four-node CPU sampling, real hwmon data where available and headless browser render PASS.
- Combined resume state is recorded in `docs/checkpoints/ANDY_OPS_GMAIL_CHECKPOINT_20260921.md`.
- PR #150 (`ops: add Andy Ops live supervisor V1`) is merged into main.

## Distributed validation control-plane rule

- Heavy validation is explicitly assigned to the distributed CI worker pool when the orchestrator is present.
- Repository agent instructions prohibit silent local fallback for full PostgreSQL, migration-heavy and full-suite validation on the control plane.
- `scripts/postgres_test_harness.sh` fails closed on a control-plane host unless an operator explicitly sets the break-glass override.
- Quick targeted diagnostics, lint and diff checks remain appropriate locally.

## Gmail automatic polling scheduler V1 — candidate

- Issue #151 implements automatic invocation of the durable Gmail history primitive
  without adding provider I/O to the core worker loop.
- A dedicated scheduler discovers active GOOGLE/GMAIL installations in fair,
  bounded rotating batches, then gives each installation its own clean session
  and transaction.
- Successful incremental runs commit; BUSY, STALE, authorization races and other
  failures roll back independently. STALE installations are quarantined for the
  scheduler process lifetime and are never silently reseeded.
- Scheduler, runner and connect boundaries remain separately gated. New scheduler
  settings default disabled; this candidate does not enable flags, deploy runtime
  or call real Gmail.
- Local targeted validation on the AGT used the existing Gmail review test image:
  62 Gmail scheduler/history/runner tests passed, then 172 scheduler/config/runner
  contract tests passed. Ruff passed. PostgreSQL-heavy certification remains
  delegated to CI01/CI02/CI03 after publication.
- See issue #151 and [Gmail Product Runner](docs/architecture/gmail-product-runner-v1.md).

## Gmail History Cursor — merged with concurrency hardening

- PR #149 is merged. It adds an installation-owned nullable Gmail history cursor
  and bounded incremental metadata ingestion. First execution seeds the current
  mailbox baseline without backfill; stale history fails closed.
- Account replacement clears the cursor; same-account reconnect preserves it.
- A concurrency review confirmed a real application-level reconnect deadlock in
  the first candidate: runner held ProviderAuthorization while ingress waited for
  Tenant, while reconnect held Tenant waiting for ProviderAuthorization.
- The candidate now uses one transaction-scoped, immutable Gmail slot advisory
  gate before authority row locks. Runner acquisition remains fail-fast BUSY;
  connect/reconnect/replacement/disconnect use the same ordering. Neutral ingress
  still independently revalidates tenant/binding/credential authority.
- Pre-hardening regression evidence: 2139 default tests passed with 434 PostgreSQL
  deselected, and the existing 432-test PostgreSQL suite passed.
- Concurrency hardening review evidence: 72 targeted Gmail tests passed; four real
  PostgreSQL contention tests passed (reconnect, account replacement, disconnect,
  first-connect/rollback), plus two history PostgreSQL tests and one schema test.
- Final post-hardening distributed PostgreSQL certification passed on the worker
  pool: 438 tests total in 113 s wall time (CI01 102, CI02 145, CI03 191). The
  first shard run exposed a pre-existing timing flake in platform findings; the
  exact test passed three consecutive CI02 retries and the complete distributed
  rerun then passed. No heavy fallback was executed on the AGT control plane.
- Ruff/diff checks passed in the concurrency review. No provider/live runtime was
  touched. Migration `0046_gmail_history_cursor` adds one nullable installation
  column and refuses downgrade when populated cursor data would be discarded.
- See [Gmail Product Runner](docs/architecture/gmail-product-runner-v1.md) for
  transaction ownership, advisory serialization, replay timestamps and manual/
  incremental overlap limits.


## Gmail Product Runner — governed execution hardening

- Main already includes the runner from PR #146; this increment completes its
  fail-closed validation and synthetic contract coverage without a second runner.
- Subscriber-created ProviderAuthorization remains the only source of the refresh
  token, integration bearer and channel.email binding. Canonical installation
  identity, credential lifetime/scopes and envelope AAD are validated before I/O.
- Each run reloads persisted authorization, binding and credential state, so
  committed revocation cannot be bypassed by an older ORM identity-map entry.
- Stable runner errors discard external exception chains; token-bearing value
  objects hide secrets from repr. Authenticated HTTP refuses redirects and the
  connector enforces the actual polling bound even on oversized ID lists.
- Targeted validation: 147 runner/transport tests and 73 Gmail/OAuth/provider-secret
  regressions passed; Ruff, compileall, generated SDK checks and diff-check passed.
  The full default suite passed: 2099 tests, with 432 PostgreSQL tests excluded
  for the separate distributed gate. Tests used a disposable Python 3.12 image
  with matching dependency pins, network disabled and a read-only source mount.
- Repository secret scan passed. Published review:
  [PR #147](https://github.com/escossio/attention-router/pull/147); its current-head
  checks track Public CI, distributed PostgreSQL and CodeQL certification.
- No schema, runtime flags, live Gmail, deployment or merge is part of this work.
- See [Gmail Product Runner](docs/architecture/gmail-product-runner-v1.md).

## Gmail Product Connection — physical E2E complete

- The real Android subscriber flow is proven through Google consent, server
  authorization-code exchange, Gmail profile lookup, encrypted provider
  authorization persistence and canonical `channel.email` binding creation.
- Safe diagnostics proved the prior provider failure as HTTP 403
  `PERMISSION_DENIED` with reasons `SERVICE_DISABLED,accessNotConfigured`: the
  Gmail API provider service was not enabled/configured for the Google Cloud
  project. After provider configuration was corrected, connect returned HTTP
  200 and the Android UI reached `Gmail connected.`.
- Cold reopen passed: authenticated bootstrap returned HTTP 200 and Gmail status
  returned HTTP 200 `CONNECTED` without a new Google authorization ceremony.
- Database proof found one active ProviderAuthorization, one active
  `channel.email` binding, one active digest-only IntegrationCredential and one
  encrypted provider-secret envelope row.
- Profile HTTP failures expose only bounded allowlisted provider reason/status
  enums; OAuth material and application data are not logged.
- All required PR #142 checks passed, including distributed PostgreSQL (432
  tests across CI01/CI02/CI03), Python/transport tests, Docker build, secret
  scan and CodeQL.
- No OAuth scope, Android contract or database schema change was required.
- See [`docs/checkpoints/GMAIL_PRODUCT_CONNECTION_E2E_20260921.md`](docs/checkpoints/GMAIL_PRODUCT_CONNECTION_E2E_20260921.md).

## Human Auth Continuation Grant V0.3A — live proof complete

- Backend implementation is merged and available from `05d5f7d462bf6d09eb5c765177f96329ac863480`.
- Android V0.3A implementation is merged in `escossio/andy-android` at `a0f4ce7196b7f9fd40bbbb66ce9fcbc3c9f9b8d4`.
- The public Client API surface at `https://api.escossio.com` exposes the Human Identity challenge, legacy `/verify`, and V0.3A `/verify-and-continue` routes over valid TLS.
- A physical Android live proof completed successfully: challenge returned HTTP 201 and `/verify-and-continue` returned HTTP 200 after real Google account selection.
- The Android UI reached `Human identity validated.`; the backend Human Auth transaction finished `VERIFIED`.
- Exactly one `DEVICE_BOOTSTRAP` continuation grant was created for the proof. The backend persisted only the token digest; the grant remained `ACTIVE`, with `consumed_at` and `revoked_at` unset.
- No continuation token matching the `hcg_` credential format was found in the app's private persisted files.
- The proof stopped at the V0.3A boundary: no device bootstrap, tenant, membership, enrollment, or session was created.
- See [`docs/checkpoints/V03A_LIVE_PROOF_20260917.md`](docs/checkpoints/V03A_LIVE_PROOF_20260917.md).

## Human Identity V1 HTTP frontier — complete

- Human Identity challenge issuance, real Google verification, legacy `/verify`, and V0.3A `/verify-and-continue` are implemented and validated.
- The real Google sign-in path was proven before V0.3A, and the continuation-grant path is now proven end to end.
- The next product frontier must begin from the existing `ACTIVE` `DEVICE_BOOTSTRAP` grant rather than re-running or redesigning Human Identity.

## Freeze checkpoint — Phase 4F deferred

The project is intentionally frozen at the Phase 4E public implementation baseline while development proceeds in other layers. Phase 4F (public API contract / developer experience) has partial local work but is **not complete**, has no remote PR/branch, and must not be resumed automatically.

The exact freeze boundary, completed local Phase 4F work, remaining gates, and resume rules are recorded in [`docs/checkpoints/PHASE4F_FREEZE_20260909.md`](docs/checkpoints/PHASE4F_FREEZE_20260909.md).

Canonical implementation baseline at freeze: `4283a9db879510038f60f12e025f87fcd277e731`.

No core product logic, database schema, migrations, live runtime, database, or immutable `v0.1.0` tag was changed by this checkpoint.

## Architecture governance

- Phase 4E merged in `e643f634247f5e918d1b519607e6333f5ca13480`.
- Public ADR index and supplementary decisions cover authority, approval,
  outbox, delivery evidence, memory, providers and browser-origin trust.
- Public threat model, trust-boundary diagram and directional roadmap are
  available; implemented, planned and conceptual scope remains separated.
- No core product logic, database schema, migrations, runtime or immutable
  `v0.1.0` tag was changed.

## Public prerelease baseline

- Main: current protected branch; Phase 4B merge SHA is recorded in the final scorecard.
- Security fix remains traceable as `31d3f8b5cab44928ded455d80a72ba3b57e49431`.
- Main is protected with seven required checks and conversation resolution.
- Dependabot is active for pip, npm, and GitHub Actions, targeting `main` weekly.
- CodeQL High open: `0`; alert #3 is dismissed with technical justification.
- Verification: Python `986`, Transport `228`, PostgreSQL `325`.
- Public CI, Docker build, Secret Scan, and CodeQL passed after merge.
- Runtime and database were not mutated by this professionalization work.

## Release state

The immutable `v0.1.0` public prerelease targets
`7e6faa89b21e1c0f21d80c34fb3e1cf02f26c805`. Its public GHCR image is amd64
only and has an immutable digest recorded in the release assets. The release
contains an SPDX JSON SBOM, checksums, and artifact/build provenance plus SBOM
attestations. The deterministic offline architecture demo and social preview
assets are available in the repository.

No live runtime, real provider credential, real WhatsApp account, real
conversation, or real database was used or mutated.

## Portfolio demo

The public portfolio demo is available as a reproducible, silent 72-second
H.264 video at `docs/assets/demo/attention-router-demo.mp4`, with a 1920x1080
poster beside it. It is synthetic-only and offline: no real names, phone
numbers, messages, hostnames, private paths, secrets, or provider calls are
used. The renderer is `scripts/render_public_demo.py` and the README links to
the assets and `docs/demo.md`.
