# Project status

Updated: 2026-09-25

## Native OpenTelemetry Transport → Ingress — runtime certified

- PR #191 serialized recovery after existing-page attach; PR #192 fixed compatibility with Puppeteer's immutable ESM namespace while preserving canonical-page revalidation and fail-closed authority. Both merged through protected main with all required checks green; #192 passed 444 distributed PostgreSQL tests.
- Runtime source `fedd841bb660f0ff820ae0ff218d55b18e7a3ec8` is READY with native OTel enabled. Internal Ingress remains READY with native OTel on its separately deployed release.
- The initial attach failure was rolled back safely. The corrected rollout preserved the browser PID/session across both attempts and restored connected client and owner authority READY.
- A real inbound canary was delivered and forwarded. Both components exported after the canary through the source-restricted host OTLP edge, and Tempo returned the native traces.
- Direct span-ID checks proved the Transport receive → attempt → remote Ingress chain and the separate canonical message root with its exact Span Link. Andy correlation identity and OTel trace identity remain distinct.
- Native attributes and the complete trace privacy audit PASS. The independent ROC reconstruction bridge remained healthy without restart and produced a recent trace.
- Sampling remains `parentbased_traceidratio` at `1.0`; steady-state policy remains a separate operational decision. Raw evidence is retained privately; the public record is sanitized.
- See [runtime certification, 2026-09-25](docs/observability/NATIVE_OTEL_RUNTIME_CERTIFICATION_20260925.md).

## Andy Ops Live Supervisor V1

- Added a sanitized, reproducible LAN-only operational panel package under `ops/provisioning/andy-ops-panel/`.
- Compute view shows AGT/CI01/CI02/CI03 CPU total + per-core btop-like meters, CPU temperature when exposed, current CI task, elapsed time and recent distributed dispatches.
- Chat/Console view observes the concrete Chat -> Remote Desktop Commander -> AGT channel using the plugin's persistent JSONL tool history and active console descendants; it does not claim to observe ChatGPT UI internals.
- V1 is read-only, has no production database access, and public files contain no private LAN address or credentials.
- Runtime proof completed on the control plane: systemd active, health PASS, live four-node CPU sampling, real hwmon data where available and headless browser render PASS.
- Combined resume state is recorded in `docs/checkpoints/ANDY_OPS_GMAIL_CHECKPOINT_20260921.md`.
- PR #150 (`ops: add Andy Ops live supervisor V1`) is merged into main.

## Artifact Store V1 — merged

- PR #154 / issue #153 fill the byte-storage boundary intentionally left open by Artifact Plane V0.
- Added a provider-neutral ArtifactObjectStore protocol and a local immutable content-addressed backend.
- Physical object paths are tenant scoped through a hashed tenant namespace; source filenames and provider metadata never control paths.
- Writes are atomic/fsync-backed and existing objects are revalidated before reuse. Reads fail closed on wrong reference, size/hash mismatch, symlink/non-regular object or tenant mismatch.
- Added stage_artifact_receipt() to persist bytes first and then register the existing canonical Artifact/Receipt identity. PostgreSQL still stores metadata only.
- Added internal tenant-scoped read by artifact_id; no public download/share route, parsing, OCR, archive extraction or content execution is introduced.
- Configuration is default-off with a 32 MiB per-object default and a bounded 1 GiB hard configuration ceiling.
- Local focused validation: Ruff PASS and 74 Artifact Plane/store/integration/config tests PASS.
- PR #154 merged Artifact Store V1 with all repository gates green; no runtime flag enablement, deployment or live filesystem migration occurred.
- Issue #155 is the current consumer: Gmail attachment download -> Artifact Store staging -> canonical artifact_ids.

## Gmail attachment ingestion V1 — merged

- Issue #155 adds explicit attachment-capable read authority without widening mutation rights: exact `gmail.metadata` remains the legacy minimum profile and exact `gmail.readonly` enables governed attachment ingestion.
- Gmail MIME discovery uses a bounded fields projection and never requests snippet or MIME `body.data`. Unexpected body data, excessive depth/count or inline attachment data without a provider attachment id fail closed.
- Attachment downloads are base64url-validated and bounded per attachment and per message before Artifact Plane staging.
- Artifact bytes and receipts commit in an independent transaction before Neutral Ingress. Cursor rollback therefore cannot erase already-admitted artifact identity; retries resolve to the same canonical `artifact_id`.
- Canonical e-mail events carry deduplicated `artifact_ids` only. Provider attachment ids, storage providers/references, filenames and bytes do not enter the event payload.
- All new runtime controls remain default-off. No live OAuth re-consent, Gmail call, Artifact Store enablement or deployment is part of this candidate.
- Synthetic validation includes metadata-regression coverage, exact readonly authority, bounded provider parsing/download, ambiguous externalized-body rejection, stable replay, runner E2E, and incremental cursor rollback with durable artifact proof.
- Pre-hardening focused gate: Ruff PASS, compileall PASS and 333 Gmail/Artifact/Integration/config tests PASS.
- Post-hardening gate after ambiguous externalized-body rejection: Ruff PASS, compileall PASS and 289 affected-surface tests PASS.
- PR #156 merged the Gmail attachment pipeline and PR #158 merged the ambiguous MIME-body hardening; both completed CodeQL, distributed PostgreSQL, secret scan, Docker, Python, transport and analysis gates successfully.

## WhatsApp inbound media Artifact V1 — merged and live-proven

- Issue #159 / PR #160 route signed, explicit-tenant WhatsApp media into canonical Artifact/Receipt evidence while preserving the legacy voice-transcription path.
- PR #163 fixed the PostgreSQL Resource-before-Artifact FK ordering exposed by the first real media proof and added a PostgreSQL-marked regression.
- Exact-SHA distributed certification after the fix passed 439 PostgreSQL tests (CI01 115 / CI02 136 / CI03 188) plus the repository-native checks.
- Live proof on 2026-09-22 used a real inbound WhatsApp PDF: the pending notification retried after the fix and converged to exactly one Resource, one Artifact, one channel.whatsapp Receipt and one immutable SSD object with matching size.
- The live Artifact Store is tenant-scoped and remains the source-of-truth byte layer; filename remains metadata only.
- Artifact Understanding is intentionally a separate derived layer rather than changing Artifact identity or executing received content.

## Artifact Understanding V1 — candidate

- Issue #166 adds governed derived understanding for inbound WhatsApp images and PDF documents without changing the immutable Artifact Store contract.
- Supported V1 inputs are JPEG, PNG, WebP, GIF and PDF. Bytes are read through the tenant-scoped Artifact Store and passed to a multimodal provider; PostgreSQL never stores the raw file.
- The OpenAI Responses provider sends images as bounded input_image data URLs and PDFs as bounded input_file data URLs with high page-image detail. Artifact content is explicitly untrusted data and cannot grant authority or supply system instructions.
- Derived state persists summary, extracted/visible text, visual description, key facts, detected language, truncation state and provider/model/prompt provenance.
- Decision queues fail closed: image/document decisions wait in WAITING_ARTIFACT_UNDERSTANDING until a READY derivation exists; FAILED understanding cancels the media-dependent decision rather than guessing around the file.
- Effective conversation text includes the persisted derivation behind an explicit untrusted-attachment boundary, so later turns retain what Andy read without mutating the original inbound event.
- Same tenant/artifact/provider/model/prompt READY results are reused across repeated deliveries; the provider is not called twice for identical canonical content under the same analysis profile.
- Runtime controls are default-off and require Artifact Store plus OPENAI_API_KEY.
- Current evidence: Ruff/compileall PASS, 77 affected unit/integration tests PASS, and a disposable PostgreSQL 16 migration + queue/worker proof PASS. Full exact-SHA distributed certification follows publication.

## Distributed validation control-plane rule

- Heavy validation is explicitly assigned to the distributed CI worker pool when the orchestrator is present.
- Repository agent instructions prohibit silent local fallback for full PostgreSQL, migration-heavy and full-suite validation on the control plane.
- `scripts/postgres_test_harness.sh` fails closed on a control-plane host unless an operator explicitly sets the break-glass override.
- Quick targeted diagnostics, lint and diff checks remain appropriate locally.

## Gmail automatic polling scheduler V1 — merged

- PR #152 / issue #151 implement automatic invocation of the durable Gmail history primitive
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

## Native OpenTelemetry audit — 2026-09-24

- Read-only native OpenTelemetry audit completed against the current runtime evidence and preserved in [docs/observability/AUDITORIA_OTEL_NATIVO_20260924.md](docs/observability/AUDITORIA_OTEL_NATIVO_20260924.md).
- No native tracing implementation, runtime change, restart, migration, database mutation, push or PR was performed by the audit.
- The audit found an existing partial native OpenTelemetry foundation and recommends hardening that foundation before enabling the Transport → Ingress boundary.
- First hardening gate: attribute/resource allowlist, removal of full exception capture and duplicate capture, no-op fallback preserving functional exceptions, invalid-context isolation, and separate exporter/flush timeouts.
- Native OpenTelemetry implementation must proceed from a clean GitHub-based worktree and be certified by pull request checks before runtime rollout.

## Native OpenTelemetry first hardening gate — 2026-09-24

- Gate 1 locally certified: explicit key/value allowlists for spans and Resources, sanitized/idempotent error metadata, fail-open no-op fallback, isolated context extraction/restoration and separate exporter/flush timeouts. No new functional instrumentation.
- Working on `feat/native-opentelemetry-v1`; the pre-existing audit and status changes are preserved. Local ignored backups were made before editing.
- Expanded `tests/test_tracing.py` with synthetic privacy checks across exported surfaces, original exception identity/chain preservation, failure injection, valid/invalid context, disabled/global-provider isolation, configuration parsing, bounded batch/flush and concurrent initialization cases.
- Validation: targeted `compileall`, `git diff --check` and Ruff 0.6.3 PASS; offline `tests/test_tracing.py` 132/132 PASS after the known SQLite metadata preload. PR #185 was merged into `main` as `296eb86647578f7eef08973de569743654becfbd` after all required GitHub checks passed, including the repository Python suite and the distributed PostgreSQL gate (444 tests).
- Residuals: allowlists intentionally drop unapproved values; SDK shutdown/retry total duration is not certified by the independent flush budget. No Collector/Tempo or E2E certification.
- No dependency pins, runtime, containers, Generic Worker, database or Transport changes were part of Gate 1.

## Native OpenTelemetry Internal Ingress admission — Etapa 1A — 2026-09-24

- Candidate branch: `feat/native-otel-ingress-admission-v1`, based on merged Gate 1 at `296eb86647578f7eef08973de569743654becfbd`.
- Internal Ingress now accepts optional W3C `traceparent` only after HMAC, payload and tenant validation, passes it explicitly across the dedicated executor, and creates `ingress.accept` as a child of a valid remote attempt.
- Canonical `attention.message` starts from explicit empty context, receives a Link to the remote attempt, and records the Andy `roc.correlation_id` only after the functional receive path creates or recovers it. Replay keeps the same Andy correlation while distinct HTTP attempts may have distinct trace identities.
- Invalid/absent carriers fail open to clean local traces; unauthenticated requests cannot create spans from a supplied trace header. Body, HMAC, payload, idempotency, database schema and functional correlation semantics are unchanged.
- Local targeted validation: `tests/test_internal_ingress.py` + `tests/test_tracing.py` = 156 PASS with the known SQLite model preload; Ruff on the changed Python files = PASS. The normal targeted run still exposes the pre-existing `human_identities` fixture-registration defect before affected tests execute.
- PR #186 was merged into `main` as `9d53b755be36986d9f46857bd1d7bd90368c4492` after all eight checks passed, including the full Python suite and distributed PostgreSQL gate (444 tests). No runtime rollout was performed by the merge.

## Native OpenTelemetry Transport attempt — Etapa 1B — 2026-09-24

- Candidate branch: `feat/native-otel-transport-attempt-v1`, based on merged Etapa 1A at `9d53b755be36986d9f46857bd1d7bd90368c4492`.
- Transport uses manual OpenTelemetry only: `@opentelemetry/api` 1.9.1, core/resources/sdk-trace 2.11.0 and OTLP HTTP/protobuf exporter 0.222.0. No automatic instrumentation, global context manager, Puppeteer hook or whatsapp-web.js hook is added.
- `transport.receive` wraps the bridge operation once. Immediate `transport.ingress_attempt` is an explicit child; background spool retries without transient context start a clean local attempt trace. Durable context across restart remains a later stage.
- The persisted spool JSON and HMAC contract are unchanged. The exact body Buffer is signed and sent; W3C `traceparent` is injected only as a lateral HTTP header, with no `tracestate` or `baggage`.
- Resource identity is fixed to `service.name=attention-router-transport`; span attributes are closed to finite operational values. Exception messages, payload, phone identifiers and secrets are not exported.
- Tracing disabled, missing endpoint or invalid telemetry configuration degrades to no-op. Exporter failures are sanitized and cannot re-run or replace the functional operation. Batch export remains off the per-message functional path.
- Local validation on the AGT: focused tracing/bridge/spool suite 17/17 PASS, including bounded flush and explicit Resource privacy checks; full local Transport suite 243/243 PASS; `node --check` on changed JavaScript and `git diff --check` PASS. The AGT shell is Node 20 while the package requires Node >=22.12, so GitHub Actions remains the authoritative runtime certification.
- No production restart, Collector/Tempo rollout, database/migration, Generic Worker or frozen release change is part of this candidate.
