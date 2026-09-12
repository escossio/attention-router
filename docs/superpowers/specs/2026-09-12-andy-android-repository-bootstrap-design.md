# Andy Android Repository Bootstrap — Architectural Design

Status: **approved design, not implemented**  
Date: 2026-09-12  
Planning authority: `escossio/attention-router`  
Target repository: `escossio/andy-android`

This document defines how the public Android application repository is to be born. It complements `2026-09-11-andy-android-foundation-design.md`; it does not replace the platform/mobile architecture already approved there.

No Android application repository, Gradle project, Kotlin source, runtime integration, deployment, provider connection, tenant, device enrollment, WhatsApp pairing, Google authorization, location collection, or Home Assistant integration is implemented by this document.

## 1. Goal

Create `escossio/andy-android` as a public repository that is structurally governed from its first commit, so later Codex/agent work cannot invent arbitrary folders, dependencies, authority boundaries, or integration paths.

The repository starts with governance, documentation, architecture zones, blocking CI, and machine-readable frontiers. Functional Android code comes only in a later frontier.

The permanent product boundary is:

`andy-android -> Kotlin SDK -> Client API -> attention-router`

`attention-router` remains the authoritative backend and contract owner. The Android application must never depend on PostgreSQL, containers, AGT01, a specific VPS, local IP addresses, internal ingress, provider internals, or server implementation details.

## 2. Repository identity

- Repository: `escossio/andy-android`
- Visibility: **public from the first commit**
- License: **Apache License 2.0**
- Display name: **Andy**
- Android application ID: `io.github.escossio.andy`
- Initial Kotlin namespace: `io.github.escossio.andy`
- Default branch: `main`

The README must state clearly that Andy Android is the mobile client product and Attention Router is the authoritative platform/backend.

The first commit is a one-time repository bootstrap exception because an empty repository cannot already enforce its own CI and branch protection. That first commit contains governance and repository structure only, never functional Kotlin. After its workflows have produced real passing check contexts, `main` is protected and all subsequent functional changes use branches and pull requests.

## 3. Bootstrap strategy

The chosen strategy is **repository skeleton + guardrails + CI, without functional Kotlin code**.

Rejected alternatives:

1. A README-only repository, because it would let later agents invent the structure.
2. A fully compilable Android app in the first commit, because it would mix governance with implementation before the repository can enforce its own boundaries.

The bootstrap therefore happens in two phases:

1. **Governance bootstrap** — repository structure, scoped instructions, machine-readable frontier policy, architecture guard, secret scanning, documentation, and branch protection.
2. **First functional frontier** — `android-app-bootstrap-v1`, which may introduce the minimum Gradle/Kotlin/Compose project required to compile and test a trivial Android shell.

## 4. Repository zones

The repository begins with these top-level responsibility zones:

- `app/` — Android application shell, lifecycle, composition root, navigation/theme wiring, and final application packaging.
- `core/` — platform-neutral application abstractions and shared models. No Android framework calls and no raw transport construction.
- `sdk/` — the only application-facing boundary for communicating with Attention Router Client API contracts and transport.
- `features/` — user-facing feature implementations. Features do not build raw URLs, attach auth headers, or call provider APIs directly.
- `capabilities/` — Android-native capability providers such as location, notifications, camera/QR, microphone, and sensors.
- `integrations/` — installation/setup UX and provider-specific mobile interaction boundaries such as WhatsApp pairing, Google authorization handoff, or Home Assistant setup. Integration code must not become core authority.
- `data/` — local persistence, encrypted caches, local models, and schema/migration responsibility.
- `sync/` — durable outbox, pull/push synchronization, freshness, retry/idempotency coordination, and offline reconciliation.
- `docs/` — architecture, specifications, plans, and contributor-facing design material.
- `.github/architecture/` — trusted architecture policy and machine-readable frontier manifests.
- `.github/workflows/` — CI workflows.
- `scripts/architecture/` — architecture guard implementation.

Git does not preserve empty directories. During governance bootstrap, responsibility zones may contain only scoped `AGENTS.md` files or equivalent non-functional governance documents. This does not authorize later code creation in those directories.

## 5. Scoped repository instructions

The root `AGENTS.md` establishes global invariants. Each responsibility zone receives a scoped `AGENTS.md` describing what belongs there, what may be imported, and what is forbidden.

At minimum the instructions enforce these invariants:

- New functional work must belong to an explicitly trusted frontier.
- Existing directory presence does not grant permission to create arbitrary files.
- A frontier cannot modify its own guard, manifest, workflow, or scoped instructions.
- A generic status-file convention does not expand an allowlist.
- When a required path or dependency is outside the frontier, the agent stops with `FRONTIER_EXPANSION_REQUIRED`.
- Real credentials, conversations, locations, tenant identifiers, device identifiers, infrastructure inventories, phone numbers, and provider payloads must never enter Git. Test fixtures are synthetic.
- `features/**` cannot perform raw HTTP or provider calls.
- `app/**` cannot own API endpoint construction or transport credentials.
- `core/**` remains free of Android framework and provider-specific dependencies unless a later explicit architectural frontier changes that rule.
- `sdk/**` is the sole normal application boundary to Attention Router network contracts.
- `capabilities/**` expose typed semantic device capabilities; there is no generic arbitrary remote-command facility.
- `integrations/**` cannot grant tenant/user authority and cannot store long-lived backend/provider secrets as application authority.
- Offline/local state never becomes authoritative for sensitive external effects.

## 6. Architecture Guard

The repository includes a blocking Architecture Guard derived from the guardrail model already designed for Attention Router, adapted for a greenfield Android repository.

The trusted policy comes from the base/default branch. Candidate pull-request content is inspected as data and is never imported or executed merely to evaluate policy.

The guard checks at least:

- exact allowed paths for a frontier;
- forbidden governance mutation;
- required artifacts;
- forbidden dependency direction/imports;
- forbidden content patterns and obvious secret material;
- known governed territory touched by an unassigned branch;
- branch/frontier identity;
- syntactic validity of trusted policy and candidate source metadata.

Stable failure classes should include equivalents of:

- `ARCH_GOVERNANCE_MUTATION`
- `ARCH_PATH_NOT_ALLOWED`
- `ARCH_DEPENDENCY_FORBIDDEN`
- `ARCH_BOUNDARY_CROSSING`
- `ARCH_REQUIRED_ARTIFACT_MISSING`
- `ARCH_FRONTIER_UNKNOWN`
- `ARCH_POLICY_INVALID`
- `ARCH_CONTENT_FORBIDDEN`

Greenfield policy is fail-closed for governed implementation territory. There is no need for broad legacy exceptions in this repository.

## 7. CI and branch protection

The governance bootstrap installs CI that can run before functional Android source exists.

Initial required checks should cover:

- Architecture Guard;
- secret scanning;
- governance/policy tests;
- repository hygiene/document validation where useful.

After the first functional Android frontier lands, CI expands to include the Android build/test/lint checks appropriate to the chosen Gradle project.

The bootstrap sequence is:

1. Create public repository and governance-only first commit.
2. Let the bootstrap workflows run on that commit.
3. Confirm the real check context names and successful results.
4. Enable `main` protection using those real checks; do not guess context names in advance.
5. Require pull requests and required status checks for later work.
6. Do not weaken or remove protections merely to merge a feature.

CodeQL or equivalent language analysis is enabled when there is meaningful supported source to analyze; the bootstrap must not pretend an empty source tree has useful code-analysis coverage.

## 8. Android technical baseline for the first functional frontier

The following baseline is approved for `android-app-bootstrap-v1`:

- `minSdk = 28` (Android 9)
- `targetSdk = 36`
- `compileSdk = 36`
- Android Gradle Plugin `9.4.0`
- Gradle `9.6.0`
- JDK `17`
- Kotlin using the modern AGP 9.x integrated Kotlin support where applicable
- Jetpack Compose using stable releases only
- Compose BOM `2026.08.00`
- Gradle Kotlin DSL (`*.gradle.kts`)
- Version Catalog (`gradle/libs.versions.toml`)

No dependency is added merely because it is common in Android projects. Hilt, Room, Retrofit, KSP, provider SDKs, analytics SDKs, authentication SDKs, location SDKs, or similar libraries require a concrete frontier that needs them.

The first functional frontier may create only enough project structure to compile, run unit tests, and package a trivial app shell. It must not implement login, Client API calls, location, WhatsApp, Home Assistant, Google integration, persistence, background sync, push, voice, or production signing.

## 9. First functional frontier

The first implementation frontier is named:

`android-app-bootstrap-v1`

Its purpose is to prove the repository toolchain and module boundaries, not product behavior.

The frontier must explicitly enumerate every file it may create or modify. It may introduce the minimal Gradle wrapper/configuration, version catalog, Android manifest/resources, one trivial Compose activity/application shell, and minimal tests required to prove the baseline.

It must not be allowed to edit:

- `.github/architecture/**`
- `scripts/architecture/**`
- `.github/workflows/**`
- root or scoped `AGENTS.md`
- security/governance documents

If implementation discovers that the approved file set, module boundary, dependency set, SDK level, or CI surface is insufficient, it stops with `FRONTIER_EXPANSION_REQUIRED`; governance is changed and reviewed separately before feature work continues.

## 10. Data, error, and authority behavior at repository level

This bootstrap does not implement runtime flows, but it freezes repository-level behavior for later work:

- network and provider failures are represented through typed SDK/integration boundaries, not ad-hoc UI exceptions;
- retries must be explicit and idempotency-aware;
- stale/offline data is never presented as current authority;
- local caches/outboxes are tenant-scoped when they are later implemented;
- push/notification payloads are attention signals, never sufficient authority for sensitive effects;
- Android OS permission is distinct from tenant/platform authorization;
- capability and integration code fails closed when authority/state is ambiguous;
- device/provider secrets are never committed to Git and must later use appropriate Android/backend secret boundaries.

## 11. Testing strategy

Governance bootstrap tests focus on the guard itself:

- allowed frontier/path passes;
- path outside allowlist fails;
- frontier attempting to modify governance fails;
- unassigned branch touching governed implementation territory fails;
- forbidden dependency/import direction fails;
- synthetic fixture policy violations fail where mechanically detectable;
- candidate content cannot cause the guard to execute candidate Python/Kotlin/shell code;
- malformed trusted frontier policy fails closed;
- legitimate governance-mode changes are evaluated using governance-specific rules.

`android-app-bootstrap-v1` adds tests proving:

- Gradle configuration resolves under the pinned JDK/toolchain;
- the app module compiles for the approved SDK baseline;
- unit tests run successfully;
- Compose/app shell source remains within the exact frontier;
- no unapproved Android/provider/network dependency appears in the dependency graph.

Instrumentation/emulator tests are not required merely to prove the first shell unless the implementation reveals a concrete need.

## 12. Bootstrap completion criteria

Governance bootstrap is complete only when all of the following are true:

- `escossio/andy-android` exists and is public;
- first commit contains no functional Kotlin implementation;
- Apache-2.0 license is present;
- README clearly documents the Android-client vs Attention-Router-backend boundary;
- root and scoped `AGENTS.md` files define the repository zones;
- architecture manifest/guard exists and has negative tests;
- secret scanning is active;
- bootstrap CI has produced real passing check contexts;
- `main` protection requires PRs and the intended status checks;
- no real secret, user data, conversation, location, phone number, tenant/device identity, or infrastructure inventory appears in Git;
- `android-app-bootstrap-v1` is defined but has not yet been implemented as part of the governance bootstrap.

The next architectural/implementation step after this bootstrap is a separate, guarded implementation plan for `android-app-bootstrap-v1`.
