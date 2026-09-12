# Andy Android Repository Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `escossio/andy-android` as a public, governance-first Android repository with blocking architecture guardrails, secret scanning, CI, scoped repository instructions, branch protection, and a predefined `android-app-bootstrap-v1` frontier, without adding functional Kotlin code.

**Architecture:** The new repository is born in two phases. This plan implements only the governance bootstrap: repository identity, responsibility zones, trusted frontier policy, a base-trusted architecture guard, secret scanning, CI, and protected `main`. The first functional Android code remains a later, separate frontier. `attention-router` remains the authoritative backend and contract owner; `andy-android` is only the Android client product.

**Tech Stack:** Git, GitHub CLI/API, GitHub Actions, Python 3 standard library for bootstrap guard/scanner tests, JSON policy manifests, Markdown governance docs. Android baseline reserved for the later feature frontier: minSdk 28, targetSdk 36, compileSdk 36, AGP 9.4.0, Gradle 9.6.0, JDK 17, stable Jetpack Compose, Compose BOM 2026.08.00, Kotlin DSL, Version Catalog.

**Spec:** `docs/superpowers/specs/2026-09-12-andy-android-repository-bootstrap-design.md`

## Global Constraints

- Target repository is exactly `escossio/andy-android` and must be public from its first pushed commit.
- License is Apache-2.0.
- Android display name is `Andy`.
- Android application ID and initial Kotlin namespace are both `io.github.escossio.andy`.
- `attention-router` remains the authoritative backend; the client must not depend on PostgreSQL, containers, AGT01, a specific VPS, local IP addresses, internal ingress, provider internals, or server implementation details.
- This bootstrap must contain no functional Kotlin, Gradle Android build, login flow, Client API call, location, WhatsApp, Home Assistant, Google integration, persistence, background sync, push, voice, signing key, or production credential.
- All fixtures and examples are synthetic. No real conversation, phone number, location, tenant/device identifier, provider payload, infrastructure inventory, token, secret, or credential may enter Git.
- New functional work requires a trusted frontier. Existing directories do not grant permission to create arbitrary files.
- Feature frontiers cannot modify `.github/architecture/**`, `scripts/architecture/**`, `.github/workflows/**`, root/scoped `AGENTS.md`, or security/governance files.
- Candidate PR content is inspected as data only. The architecture guard must never import, source, execute, or evaluate candidate Python, Kotlin, shell, Gradle, or other source.
- If a future implementation needs a path/dependency outside its trusted frontier, it stops with `FRONTIER_EXPANSION_REQUIRED`; governance changes are reviewed separately.
- A generic `STATUS.md` or repository-status convention never expands an allowlist.
- Initial Android baseline is frozen at minSdk 28, targetSdk 36, compileSdk 36, AGP 9.4.0, Gradle 9.6.0, JDK 17, stable Compose, Compose BOM 2026.08.00, Kotlin DSL, and Version Catalog.
- Do not merge or deploy anything outside this repository bootstrap.

---

## Locked File Structure

The governance bootstrap creates exactly these repository-governance areas before functional Android work:

```text
andy-android/
├── .gitignore
├── AGENTS.md
├── LICENSE
├── README.md
├── SECURITY.md
├── app/AGENTS.md
├── core/AGENTS.md
├── sdk/AGENTS.md
├── features/AGENTS.md
├── capabilities/AGENTS.md
├── integrations/AGENTS.md
├── data/AGENTS.md
├── sync/AGENTS.md
├── docs/architecture/README.md
├── .github/architecture/README.md
├── .github/architecture/frontiers/android-app-bootstrap-v1.json
├── .github/workflows/governance.yml
├── scripts/architecture/check_guardrails.py
├── scripts/security/scan_secrets.py
├── tests/architecture/test_guardrails_policy.py
├── tests/architecture/test_guardrails_git_integration.py
└── tests/security/test_secret_scan.py
```

The later `android-app-bootstrap-v1` frontier is pre-authorized to create or modify only:

```text
settings.gradle.kts
build.gradle.kts
gradle.properties
gradle/libs.versions.toml
gradle/wrapper/gradle-wrapper.properties
gradle/wrapper/gradle-wrapper.jar
gradlew
gradlew.bat
app/build.gradle.kts
app/src/main/AndroidManifest.xml
app/src/main/java/io/github/escossio/andy/MainActivity.kt
app/src/main/res/values/strings.xml
app/src/test/java/io/github/escossio/andy/BootstrapTest.kt
```

No other path is authorized by that frontier.

---

### Task 1: Preflight and Local Bootstrap Workspace

**Files:**
- Create locally during execution: `/srv/projetos/andy-android`
- Do not create remote repository yet.

**Interfaces:**
- Consumes: approved spec in `escossio/attention-router`.
- Produces: clean local Git repository on `main`, ready for governance-only commits.

- [ ] **Step 1: Verify GitHub authentication and target absence**

Run:

```bash
gh auth status
gh repo view escossio/andy-android --json nameWithOwner,visibility,defaultBranchRef
```

Expected: `gh auth status` succeeds. `gh repo view` must report that `escossio/andy-android` does not exist. If the repository already exists, stop and report its current visibility/default branch before making any mutation.

- [ ] **Step 2: Verify source planning artifacts are present on GitHub**

Run:

```bash
gh api repos/escossio/attention-router/contents/docs/superpowers/specs/2026-09-12-andy-android-repository-bootstrap-design.md?ref=docs/andy-android-repository-bootstrap-20260912 --jq .sha
gh api repos/escossio/attention-router/contents/docs/superpowers/plans/2026-09-12-andy-android-repository-bootstrap.md?ref=docs/andy-android-repository-bootstrap-20260912 --jq .sha
```

Expected: both commands return a blob SHA.

- [ ] **Step 3: Create the local bootstrap repository**

Run:

```bash
install -d -m 0755 /srv/projetos/andy-android
cd /srv/projetos/andy-android
git init -b main
git status --short --branch
```

Expected: branch is `main`, repository has no commits, worktree is empty.

- [ ] **Step 4: Record execution provenance without copying secrets**

Run:

```bash
git config user.name
git config user.email
```

Expected: both values are configured. Do not write credentials or `gh auth token` output into files or logs.

---

### Task 2: Create Repository Identity, Security Docs, and Scoped Zones

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `SECURITY.md`
- Create: `LICENSE`
- Create: `AGENTS.md`
- Create: `app/AGENTS.md`
- Create: `core/AGENTS.md`
- Create: `sdk/AGENTS.md`
- Create: `features/AGENTS.md`
- Create: `capabilities/AGENTS.md`
- Create: `integrations/AGENTS.md`
- Create: `data/AGENTS.md`
- Create: `sync/AGENTS.md`
- Create: `docs/architecture/README.md`

**Interfaces:**
- Consumes: repository identity and boundaries from the approved spec.
- Produces: the permanent physical responsibility zones and contributor/agent instructions that later guard policy enforces.

- [ ] **Step 1: Write repository identity docs**

Create `README.md` with this exact minimum content, expanding prose only if it preserves these statements:

```markdown
# Andy Android

Andy Android is the public Android client for the Andy product.

The authoritative platform/backend is [Attention Router](https://github.com/escossio/attention-router). This repository must communicate with that platform only through versioned client contracts / SDK boundaries. It must never depend on PostgreSQL, containers, AGT01, a specific VPS, local IP addresses, internal ingress, provider internals, or server implementation details.

## Status

Governance bootstrap only. No functional Android application is implemented yet.

## Identity

- App name: Andy
- Application ID: `io.github.escossio.andy`
- Kotlin namespace: `io.github.escossio.andy`
- License: Apache-2.0

## Development rule

Functional work is allowed only inside a trusted architecture frontier. If an implementation requires a path or dependency outside its frontier, stop with `FRONTIER_EXPANSION_REQUIRED` and change governance separately.
```

Create `SECURITY.md` with these required rules:

```markdown
# Security Policy

Do not commit real credentials, API keys, OAuth tokens, signing keys, conversations, phone numbers, precise locations, tenant/device identifiers, provider payloads, or infrastructure inventories.

All examples and fixtures must be synthetic.

Persistent provider credentials belong to backend/integration secret boundaries, not to this Android repository. Android signing material must never be committed.

If a secret is exposed, rotate/revoke it first, then remove it from active history and investigate the affected boundary.
```

Create `.gitignore` containing at least:

```gitignore
.gradle/
.idea/
local.properties
*.iml
build/
**/build/
.cxx/
.externalNativeBuild/
*.jks
*.keystore
*.p12
*.pem
*.key
.env
.env.*
!.env.example
.DS_Store
```

- [ ] **Step 2: Copy the canonical Apache-2.0 license from Attention Router**

Run:

```bash
gh api repos/escossio/attention-router/contents/LICENSE -H 'Accept: application/vnd.github.raw+json' > LICENSE
grep -F 'Apache License' LICENSE
grep -F 'Version 2.0, January 2004' LICENSE
```

Expected: both grep commands succeed.

- [ ] **Step 3: Write root `AGENTS.md`**

Create `AGENTS.md` with these non-negotiable instructions:

```markdown
# Contributor automation instructions

Read `README.md`, `SECURITY.md`, `docs/architecture/README.md`, and the scoped `AGENTS.md` for every area you change.

This repository is greenfield and fail-closed: functional work requires an exact trusted frontier. Directory existence is not permission to create files.

Do not commit secrets, real conversations, real phone numbers, real precise locations, tenant/device identifiers, provider payloads, signing material, or infrastructure inventories. Use synthetic fixtures only.

`attention-router` is the authoritative backend. Do not couple this client to PostgreSQL, containers, AGT01, any specific VPS, local IP addresses, internal ingress, provider internals, or backend implementation details.

A feature frontier may not modify its own architecture policy, manifest, CI workflow, root/scoped `AGENTS.md`, or security/governance documents.

A generic status-file convention does not expand a trusted frontier. Do not create or modify `STATUS.md` unless a trusted frontier explicitly allows that exact path.

If a required path, dependency, SDK, or authority boundary is outside the trusted frontier, stop with `FRONTIER_EXPANSION_REQUIRED` and report the exact path/dependency and reason. Governance changes are reviewed separately.

Candidate code must never be executed merely to validate architecture policy.
```

- [ ] **Step 4: Write scoped zone instructions**

Create each scoped file with one clear responsibility:

`app/AGENTS.md`

```markdown
# App shell boundary

`app/` owns Android lifecycle, composition root, navigation/theme wiring, and final packaging. It does not construct Client API URLs, attach auth headers, own transport credentials, call provider APIs directly, or make server authority decisions.
```

`core/AGENTS.md`

```markdown
# Core boundary

`core/` contains platform-neutral application abstractions and shared models. It must not depend on Android framework APIs, provider SDKs, raw HTTP clients, or concrete server infrastructure.
```

`sdk/AGENTS.md`

```markdown
# SDK boundary

`sdk/` is the normal application boundary to Attention Router Client API contracts and transport. UI/features must not bypass it with raw URLs, authentication headers, or ad-hoc JSON protocol logic.
```

`features/AGENTS.md`

```markdown
# Feature boundary

`features/` contains user-facing product behavior. Features may depend on approved core/SDK/capability interfaces but must not perform raw HTTP, direct provider calls, persistence hacks, or authority decisions outside their frontier.
```

`capabilities/AGENTS.md`

```markdown
# Device capability boundary

`capabilities/` implements typed Android-native capabilities such as location, notifications, camera/QR, microphone, and sensors. Android permission is not server/tenant authority. No generic arbitrary remote-command capability is allowed.
```

`integrations/AGENTS.md`

```markdown
# Integration boundary

`integrations/` contains provider-specific installation/setup UX and mobile handoff logic. Integration code cannot create tenant/user authority, become core policy, or retain long-lived backend/provider credentials as application authority.
```

`data/AGENTS.md`

```markdown
# Local data boundary

`data/` owns local persistence, encrypted caches, local models, and schema/migration responsibility. Local state is never authoritative for sensitive external effects and must remain tenant-scoped when tenant data is introduced.
```

`sync/AGENTS.md`

```markdown
# Sync boundary

`sync/` owns durable outbox, pull/push synchronization, freshness, retry/idempotency coordination, and offline reconciliation. Stale/offline data must never be promoted to current server authority.
```

- [ ] **Step 5: Write architecture overview**

Create `docs/architecture/README.md` containing:

```markdown
# Andy Android Architecture

Permanent boundary:

`andy-android -> Kotlin SDK -> Client API -> attention-router`

Permanent authority chain:

`Human Identity -> Tenant Membership -> Active Tenant -> Device -> Session -> SDK/API -> Capabilities / Channels / Integrations`

The client is offline-tolerant but not authoritative for identity, membership, approvals, policy, or sensitive external execution. Android permissions, device capabilities, tenant authorization, and execution authority remain distinct.

The initial repository zones are `app`, `core`, `sdk`, `features`, `capabilities`, `integrations`, `data`, and `sync`. Functional work is admitted only by machine-readable frontiers under `.github/architecture/frontiers/`.

The first planned feature frontier is `android-app-bootstrap-v1`; it proves only the Android toolchain and a trivial app shell.
```

- [ ] **Step 6: Verify the governance-only skeleton contains no functional source**

Run:

```bash
find . -type f -not -path './.git/*' | sort
find . -type f \( -name '*.kt' -o -name '*.kts' -o -name '*.java' -o -name 'AndroidManifest.xml' \) -print
```

Expected: first command lists only governance/docs files. Second command returns no output.

- [ ] **Step 7: Commit the repository skeleton**

Run:

```bash
git add .gitignore README.md SECURITY.md LICENSE AGENTS.md app core sdk features capabilities integrations data sync docs/architecture/README.md
git commit -m 'chore: establish Andy Android governance skeleton'
```

Expected: first commit exists on `main` and contains no functional Android source.

---

### Task 3: Add Trusted Frontier Manifest and Architecture Guard

**Files:**
- Create: `.github/architecture/README.md`
- Create: `.github/architecture/frontiers/android-app-bootstrap-v1.json`
- Create: `scripts/architecture/check_guardrails.py`
- Create: `tests/architecture/test_guardrails_policy.py`
- Create: `tests/architecture/test_guardrails_git_integration.py`

**Interfaces:**
- Consumes: root/scoped `AGENTS.md`, exact feature path list above.
- Produces: `check_guardrails.py --policy-self-check` and `check_guardrails.py --base-ref BASE --head-ref HEAD --branch BRANCH` with stable `ARCH_*` outcomes.

- [ ] **Step 1: Write failing policy tests first**

Create `tests/architecture/test_guardrails_policy.py` using `unittest`. Tests must import only the trusted local guard module and cover these exact cases:

```python
import unittest
from scripts.architecture.check_guardrails import evaluate_frontier, validate_manifest


class GuardPolicyTests(unittest.TestCase):
    def test_exact_allowed_path_passes(self):
        manifest = {
            "schema_version": 1,
            "frontier_id": "android-app-bootstrap-v1",
            "branch": "feat/android-app-bootstrap-v1",
            "allowed_paths": ["settings.gradle.kts"],
            "required_artifacts": [],
            "forbidden_content_patterns": [],
        }
        self.assertEqual(evaluate_frontier(manifest, ["settings.gradle.kts"], {}), [])

    def test_path_outside_allowlist_fails(self):
        manifest = {
            "schema_version": 1,
            "frontier_id": "android-app-bootstrap-v1",
            "branch": "feat/android-app-bootstrap-v1",
            "allowed_paths": ["settings.gradle.kts"],
            "required_artifacts": [],
            "forbidden_content_patterns": [],
        }
        errors = evaluate_frontier(manifest, ["README.md"], {})
        self.assertTrue(any(error.startswith("ARCH_PATH_NOT_ALLOWED") for error in errors))

    def test_invalid_manifest_fails_closed(self):
        errors = validate_manifest({"schema_version": 1})
        self.assertTrue(any(error.startswith("ARCH_POLICY_INVALID") for error in errors))


if __name__ == "__main__":
    unittest.main()
```

Run:

```bash
python3 -m unittest tests.architecture.test_guardrails_policy -v
```

Expected: FAIL because `scripts.architecture.check_guardrails` does not exist.

- [ ] **Step 2: Create the trusted frontier manifest**

Create `.github/architecture/frontiers/android-app-bootstrap-v1.json` with exactly this policy shape:

```json
{
  "schema_version": 1,
  "frontier_id": "android-app-bootstrap-v1",
  "branch": "feat/android-app-bootstrap-v1",
  "allowed_paths": [
    "settings.gradle.kts",
    "build.gradle.kts",
    "gradle.properties",
    "gradle/libs.versions.toml",
    "gradle/wrapper/gradle-wrapper.properties",
    "gradle/wrapper/gradle-wrapper.jar",
    "gradlew",
    "gradlew.bat",
    "app/build.gradle.kts",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/java/io/github/escossio/andy/MainActivity.kt",
    "app/src/main/res/values/strings.xml",
    "app/src/test/java/io/github/escossio/andy/BootstrapTest.kt"
  ],
  "required_artifacts": [
    "settings.gradle.kts",
    "build.gradle.kts",
    "gradle/libs.versions.toml",
    "gradle/wrapper/gradle-wrapper.properties",
    "gradlew",
    "app/build.gradle.kts",
    "app/src/main/AndroidManifest.xml",
    "app/src/main/java/io/github/escossio/andy/MainActivity.kt",
    "app/src/test/java/io/github/escossio/andy/BootstrapTest.kt"
  ],
  "forbidden_content_patterns": [
    "retrofit",
    "okhttp",
    "ktor-client",
    "dagger",
    "hilt",
    "androidx\\.room",
    "firebase",
    "com\\.google\\.android\\.gms",
    "androidx\\.work",
    "play-services-location",
    "home.?assistant",
    "whatsapp"
  ]
}
```

Create `.github/architecture/README.md` documenting:

```markdown
# Architecture Guard

Trusted frontier policy is read from the base/default branch. Candidate pull-request content is data only and must never be imported, sourced, or executed by the guard.

Feature branches must exactly match a trusted frontier's `branch` field. Governance branches use the prefix `chore/architecture-governance/` and may change only governance paths. Documentation branches may use `docs/` and may change only documentation/security text paths; they cannot touch functional implementation or policy code.

Stable outcomes include `ARCH_PASS`, `ARCH_GOVERNANCE_MUTATION`, `ARCH_PATH_NOT_ALLOWED`, `ARCH_DEPENDENCY_FORBIDDEN`, `ARCH_BOUNDARY_CROSSING`, `ARCH_REQUIRED_ARTIFACT_MISSING`, `ARCH_FRONTIER_UNKNOWN`, `ARCH_POLICY_INVALID`, and `ARCH_CONTENT_FORBIDDEN`.

If a feature needs scope outside its manifest, stop with `FRONTIER_EXPANSION_REQUIRED`; do not edit the manifest from the feature branch.
```

- [ ] **Step 3: Implement the minimal pure policy functions**

Create `scripts/architecture/check_guardrails.py`. It must expose these exact functions:

```python
def validate_manifest(manifest: dict) -> list[str]: ...
def evaluate_frontier(manifest: dict, changed_paths: list[str], candidate_text: dict[str, str]) -> list[str]: ...
def load_manifests_from_ref(ref: str) -> list[dict]: ...
def changed_paths(base_ref: str, head_ref: str) -> list[str]: ...
def candidate_text_from_ref(head_ref: str, paths: list[str]) -> dict[str, str]: ...
def main() -> int: ...
```

Implementation rules:

- `validate_manifest` requires `schema_version == 1`, non-empty string `frontier_id`, non-empty string `branch`, list `allowed_paths`, list `required_artifacts`, and list `forbidden_content_patterns`.
- `evaluate_frontier` emits `ARCH_PATH_NOT_ALLOWED:<path>` for any changed path not exactly in `allowed_paths`.
- `evaluate_frontier` emits `ARCH_GOVERNANCE_MUTATION:<path>` if a feature changes `.github/architecture/`, `scripts/architecture/`, `.github/workflows/`, any `AGENTS.md`, `SECURITY.md`, or `README.md`.
- `evaluate_frontier` applies every regex in `forbidden_content_patterns` case-insensitively to candidate text and emits `ARCH_DEPENDENCY_FORBIDDEN:<path>:<pattern>` on match.
- After evaluating a feature, every `required_artifacts` path must exist at `head_ref`; otherwise emit `ARCH_REQUIRED_ARTIFACT_MISSING:<path>`.
- `load_manifests_from_ref` uses only `git ls-tree` and `git show` against the trusted `base_ref`; it must not read candidate manifests when evaluating a PR.
- `candidate_text_from_ref` uses `git show HEAD:path` as bytes, decodes UTF-8 when possible, and skips binary/un-decodable content without executing it.
- Feature mode matches branch exactly to one trusted manifest. Unknown feature branch touching any implementation/governance path exits with `ARCH_FRONTIER_UNKNOWN`.
- Governance mode is only for branch prefix `chore/architecture-governance/` and allows changes only under `.github/architecture/`, `.github/workflows/`, `scripts/architecture/`, `scripts/security/`, `tests/architecture/`, `tests/security/`, `docs/architecture/`, plus root/scoped `AGENTS.md`, `README.md`, `SECURITY.md`, `.gitignore`, and `LICENSE`.
- Docs mode is only for branch prefix `docs/` and allows only `README.md`, `SECURITY.md`, and `docs/**`.
- `--policy-self-check` loads current worktree manifests and validates them without comparing branches.
- Success prints `ARCH_PASS` and exits 0; any violation prints every stable error line and exits 1.

- [ ] **Step 4: Run policy tests to green**

Run:

```bash
python3 -m unittest tests.architecture.test_guardrails_policy -v
python3 scripts/architecture/check_guardrails.py --policy-self-check
```

Expected: tests PASS and command prints `ARCH_PASS`.

- [ ] **Step 5: Write Git integration tests proving candidate code is never executed**

Create `tests/architecture/test_guardrails_git_integration.py` using `tempfile`, `subprocess`, and `unittest`. Tests must create temporary Git repositories and prove at least:

1. exact feature branch with allowed changed path can be evaluated;
2. unassigned branch touching `app/` fails with `ARCH_FRONTIER_UNKNOWN`;
3. feature changing `.github/architecture/frontiers/android-app-bootstrap-v1.json` fails with `ARCH_GOVERNANCE_MUTATION`;
4. feature adding forbidden dependency text fails with `ARCH_DEPENDENCY_FORBIDDEN`;
5. a candidate `gradlew` containing a shell command that would create a marker file is read as data and never executed; after guard evaluation the marker file must not exist.

The malicious candidate fixture must build the marker command dynamically inside the test and must not execute candidate content itself.

Run:

```bash
python3 -m unittest tests.architecture.test_guardrails_git_integration -v
```

Expected: all integration tests PASS.

- [ ] **Step 6: Commit architecture policy and tests**

Run:

```bash
git add .github/architecture scripts/architecture tests/architecture
git commit -m 'chore: add architecture frontier guard'
```

---

### Task 4: Add Independent Bootstrap Secret Scanner

**Files:**
- Create: `scripts/security/scan_secrets.py`
- Create: `tests/security/test_secret_scan.py`

**Interfaces:**
- Consumes: repository file tree.
- Produces: `scan_secrets.py [paths...]`, exit 0 with `SECRET_SCAN_PASS` or exit 1 with `SECRET_SCAN_FINDING:<path>:<rule>`.

- [ ] **Step 1: Write failing scanner tests**

Create `tests/security/test_secret_scan.py` with `unittest` tests for these rules:

- private-key headers (`-----BEGIN ... PRIVATE KEY-----`);
- GitHub-like token prefix with synthetic body created at runtime (`"ghp_" + "A" * 36`);
- AWS access key pattern (`"AKIA" + "A" * 16`);
- OpenAI-like secret pattern built at runtime (`"sk-" + "A" * 32`);
- harmless documentation text passes;
- scanner ignores `.git/` and common binary/build paths.

Run:

```bash
python3 -m unittest tests.security.test_secret_scan -v
```

Expected: FAIL because scanner does not exist.

- [ ] **Step 2: Implement scanner with standard library only**

Create `scripts/security/scan_secrets.py` with:

```python
def scan_text(path: str, text: str) -> list[str]: ...
def iter_candidate_files(paths: list[str]) -> list[str]: ...
def main() -> int: ...
```

Use compiled regexes for these stable rule names:

```text
PRIVATE_KEY
GITHUB_TOKEN
AWS_ACCESS_KEY
OPENAI_STYLE_SECRET
```

Requirements:

- scan UTF-8 text only;
- skip `.git/`, `.gradle/`, `.idea/`, any `build/`, binary extensions, and files larger than 2 MiB;
- never print the matched secret value; print only path and rule name;
- success prints `SECRET_SCAN_PASS`;
- findings print `SECRET_SCAN_FINDING:<path>:<rule>` and exit 1.

- [ ] **Step 3: Run scanner tests and repository scan**

Run:

```bash
python3 -m unittest tests.security.test_secret_scan -v
python3 scripts/security/scan_secrets.py .
```

Expected: tests PASS and scan prints `SECRET_SCAN_PASS`.

- [ ] **Step 4: Commit scanner and tests**

Run:

```bash
git add scripts/security tests/security
git commit -m 'chore: add bootstrap secret scanner'
```

---

### Task 5: Add Bootstrap GitHub Actions CI

**Files:**
- Create: `.github/workflows/governance.yml`

**Interfaces:**
- Consumes: guard, policy tests, secret scanner.
- Produces: real GitHub check runs named exactly `architecture-guard`, `governance-tests`, and `secret-scan`.

- [ ] **Step 1: Create workflow with least-privilege permissions**

Create `.github/workflows/governance.yml` with this structure:

```yaml
name: Governance CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  governance-tests:
    name: governance-tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: python -m unittest discover -s tests -p 'test_*.py' -v

  secret-scan:
    name: secret-scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: python scripts/security/scan_secrets.py .

  architecture-guard:
    name: architecture-guard
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - name: Validate current policy on main push
        if: github.event_name == 'push'
        run: python scripts/architecture/check_guardrails.py --policy-self-check
      - name: Evaluate pull request against trusted base
        if: github.event_name == 'pull_request'
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          HEAD_BRANCH: ${{ github.head_ref }}
        run: python scripts/architecture/check_guardrails.py --base-ref "$BASE_SHA" --head-ref "$HEAD_SHA" --branch "$HEAD_BRANCH"
```

Do not add write permissions, tokens, deployments, Android SDK setup, Gradle, signing, provider credentials, or secrets to this workflow.

- [ ] **Step 2: Validate workflow text and local bootstrap suite**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/architecture/check_guardrails.py --policy-self-check
python3 scripts/security/scan_secrets.py .
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 3: Commit CI**

Run:

```bash
git add .github/workflows/governance.yml
git commit -m 'ci: add governance bootstrap checks'
```

---

### Task 6: Final Local Gate Before Publishing the Public Repository

**Files:**
- No new files expected.

**Interfaces:**
- Consumes: all governance bootstrap commits.
- Produces: auditable proof that public history contains no functional Android code or sensitive material.

- [ ] **Step 1: Verify exact history and changed file inventory**

Run:

```bash
git log --oneline --decorate --reverse
git ls-tree -r --name-only HEAD | sort
```

Expected: only the files listed in the locked governance structure exist.

- [ ] **Step 2: Assert no functional Android source/build exists**

Run:

```bash
if git ls-tree -r --name-only HEAD | grep -E '\.(kt|kts|java)$|AndroidManifest\.xml$|gradlew$|gradle-wrapper\.jar$'; then
  echo 'BOOTSTRAP_FUNCTIONAL_SOURCE_FOUND'
  exit 1
else
  echo 'BOOTSTRAP_NO_FUNCTIONAL_SOURCE=PASS'
fi
```

Expected: `BOOTSTRAP_NO_FUNCTIONAL_SOURCE=PASS`.

- [ ] **Step 3: Run all local proof commands again**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/architecture/check_guardrails.py --policy-self-check
python3 scripts/security/scan_secrets.py .
git diff --check
git status --short
```

Expected: tests PASS, `ARCH_PASS`, `SECRET_SCAN_PASS`, `git diff --check` silent, and clean worktree.

---

### Task 7: Create `escossio/andy-android` Publicly and Push Governance History

**Files:**
- Remote repository creation only.

**Interfaces:**
- Consumes: clean local `main` from Task 6.
- Produces: public GitHub repository with governance-only history and default branch `main`.

- [ ] **Step 1: Create the public repository from the verified local source**

Run from `/srv/projetos/andy-android`:

```bash
gh repo create escossio/andy-android --public --source=. --remote=origin --push --description 'Public Android client for Andy, backed by Attention Router'
```

Expected: repository creation and initial `main` push succeed.

- [ ] **Step 2: Verify GitHub repository identity immediately**

Run:

```bash
gh repo view escossio/andy-android --json nameWithOwner,visibility,defaultBranchRef,url
```

Expected:

```text
nameWithOwner=escossio/andy-android
visibility=PUBLIC
default branch=main
```

- [ ] **Step 3: Verify remote tree still contains no functional Android implementation**

Run:

```bash
gh api repos/escossio/andy-android/git/trees/main?recursive=1 --jq '.tree[].path' | sort
```

Expected: tree matches the governance bootstrap; no `.kt`, `.kts`, `AndroidManifest.xml`, `gradlew`, or Gradle wrapper binary exists yet.

---

### Task 8: Verify Real Check Contexts, Enable Secret Scanning, and Protect `main`

**Files:**
- GitHub repository settings only.

**Interfaces:**
- Consumes: first successful `Governance CI` run on public `main`.
- Produces: protected `main` requiring PRs and the observed real check contexts.

- [ ] **Step 1: Wait for and inspect the first workflow run**

Run:

```bash
gh run list --repo escossio/andy-android --workflow governance.yml --limit 5
```

Identify the run for current `main`, then:

```bash
MAIN_SHA=$(git rev-parse HEAD)
gh api "repos/escossio/andy-android/commits/$MAIN_SHA/check-runs" --jq '.check_runs[] | [.name,.status,.conclusion] | @tsv'
```

Expected: real check runs named `architecture-guard`, `governance-tests`, and `secret-scan`, each completed with `success`. Do not configure branch protection until this is true.

- [ ] **Step 2: Verify or enable GitHub native secret scanning**

Run:

```bash
gh api repos/escossio/andy-android --jq '.security_and_analysis.secret_scanning.status // "not-reported"'
```

If status is not `enabled`, enable it with an admin-authorized repository settings call:

```bash
cat >/tmp/andy-security.json <<'JSON'
{
  "security_and_analysis": {
    "secret_scanning": {"status": "enabled"},
    "secret_scanning_push_protection": {"status": "enabled"}
  }
}
JSON

gh api --method PATCH repos/escossio/andy-android --input /tmp/andy-security.json
rm -f /tmp/andy-security.json
```

Re-read repository settings and require `secret_scanning=enabled`. If push protection is unsupported for the account/repository, report that exact API response but do not disable secret scanning or weaken CI.

- [ ] **Step 3: Configure `main` branch protection using the observed contexts**

After Step 1 proves those exact check names, create the protection payload:

```bash
cat >/tmp/andy-main-protection.json <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "architecture-guard",
      "governance-tests",
      "secret-scan"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON

gh api --method PUT repos/escossio/andy-android/branches/main/protection --input /tmp/andy-main-protection.json
rm -f /tmp/andy-main-protection.json
```

If GitHub rejects `required_approving_review_count: 0`, do not switch to a one-approval rule that the sole author cannot satisfy. Stop and report the API response so the PR requirement can be implemented with a repository ruleset that requires PRs without impossible self-approval.

- [ ] **Step 4: Read back branch protection**

Run:

```bash
gh api repos/escossio/andy-android/branches/main/protection
```

Verify:

- strict required status checks are enabled;
- all three observed check contexts are required;
- force pushes are disabled;
- deletions are disabled;
- conversation resolution is required;
- admin enforcement is enabled;
- pull-request gating is present without creating an impossible approval policy.

---

### Task 9: Prove Guard Behavior Through a Disposable PR Without Merging

**Files:**
- Temporary branch only; no merge.

**Interfaces:**
- Consumes: protected `main` and trusted guard.
- Produces: remote proof that an unauthorized implementation path is blocked by CI.

- [ ] **Step 1: Create a disposable unauthorized branch from protected main**

Run:

```bash
git switch -c test/architecture-guard-negative main
mkdir -p app/src/main/java/io/github/escossio/andy
printf '%s\n' 'synthetic unauthorized probe' > app/src/main/java/io/github/escossio/andy/UnauthorizedProbe.txt
git add app/src/main/java/io/github/escossio/andy/UnauthorizedProbe.txt
git commit -m 'test: prove architecture guard blocks unassigned implementation'
git push -u origin test/architecture-guard-negative
```

- [ ] **Step 2: Open a disposable PR and observe failure**

Run:

```bash
gh pr create --repo escossio/andy-android --base main --head test/architecture-guard-negative --title 'test: architecture guard negative proof' --body 'Disposable CI proof. This PR must not merge.'
```

Then inspect checks:

```bash
gh pr checks --repo escossio/andy-android test/architecture-guard-negative
```

Expected: `architecture-guard` fails with `ARCH_FRONTIER_UNKNOWN`; governance tests and secret scan may pass. The branch must not be mergeable because the required guard failed.

- [ ] **Step 3: Close the disposable PR and delete the branch without merging**

Run:

```bash
gh pr close --repo escossio/andy-android test/architecture-guard-negative --delete-branch
git switch main
git branch -D test/architecture-guard-negative
```

Verify `main` SHA did not change during the proof.

---

### Task 10: Final Bootstrap Verification and Handoff

**Files:**
- No new functional files.

**Interfaces:**
- Consumes: public repository, passing governance CI, protected `main`, negative PR proof.
- Produces: a final execution report proving readiness for the later `android-app-bootstrap-v1` implementation plan.

- [ ] **Step 1: Verify repository and main state**

Run:

```bash
cd /srv/projetos/andy-android
git switch main
git pull --ff-only origin main
git status --short --branch
gh repo view escossio/andy-android --json nameWithOwner,visibility,defaultBranchRef,url
```

Expected: clean `main`, public repository, default branch `main`.

- [ ] **Step 2: Verify all governance checks one last time**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/architecture/check_guardrails.py --policy-self-check
python3 scripts/security/scan_secrets.py .
MAIN_SHA=$(git rev-parse HEAD)
gh api "repos/escossio/andy-android/commits/$MAIN_SHA/check-runs" --jq '.check_runs[] | [.name,.conclusion] | @tsv'
```

Expected: local tests PASS, `ARCH_PASS`, `SECRET_SCAN_PASS`, and required remote checks successful.

- [ ] **Step 3: Verify the first functional frontier exists but has not been implemented**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('.github/architecture/frontiers/android-app-bootstrap-v1.json')
data = json.loads(p.read_text())
assert data['frontier_id'] == 'android-app-bootstrap-v1'
assert data['branch'] == 'feat/android-app-bootstrap-v1'
print('ANDROID_APP_BOOTSTRAP_FRONTIER=DEFINED')
PY

if git ls-tree -r --name-only HEAD | grep -E '\.(kt|kts|java)$|AndroidManifest\.xml$|gradlew$|gradle-wrapper\.jar$'; then
  echo 'FUNCTIONAL_ANDROID_IMPLEMENTATION_PRESENT=YES'
  exit 1
else
  echo 'FUNCTIONAL_ANDROID_IMPLEMENTATION_PRESENT=NO'
fi
```

Expected: frontier defined, functional implementation absent.

- [ ] **Step 4: Produce final execution report**

The executor must report exactly these fields with concrete values:

```text
ANDY_ANDROID_BOOTSTRAP_STATUS=PASS/FAIL
REPOSITORY=escossio/andy-android
VISIBILITY=PUBLIC
DEFAULT_BRANCH=main
MAIN_SHA=
LOCAL_PATH=/srv/projetos/andy-android
LICENSE=Apache-2.0
APPLICATION_ID=io.github.escossio.andy
ARCHITECTURE_GUARD=PASS/FAIL
GOVERNANCE_TESTS=PASS/FAIL
SECRET_SCAN=PASS/FAIL
GITHUB_NATIVE_SECRET_SCANNING=ENABLED/UNAVAILABLE
MAIN_PROTECTION=PASS/FAIL
REQUIRED_CHECKS=
NEGATIVE_PR_PROOF=PASS/FAIL
ANDROID_APP_BOOTSTRAP_FRONTIER=DEFINED
FUNCTIONAL_ANDROID_IMPLEMENTATION_PRESENT=NO
RUNTIME_MUTATION=NO
ATTENTION_ROUTER_RUNTIME_MUTATION=NO
DEPLOYMENT_EXECUTED=NO
FRONTIER_EXPANSION_REQUIRED=NO/YES
RISKS=
```

Do not implement `android-app-bootstrap-v1` in this plan. Its Gradle/Kotlin/Compose work begins only after this bootstrap has been reviewed and accepted.

---

## Plan Self-Review Record

- Spec coverage: repository identity, public visibility, Apache-2.0, responsibility zones, scoped instructions, base-trusted guard, exact first frontier, secret scanning, CI, native secret scanning, protected `main`, negative guard proof, and no functional Kotlin are all assigned to concrete tasks.
- Placeholder scan: no implementation TODO/TBD placeholders are left; dynamic values such as SHA/run IDs are derived by commands at execution time.
- Boundary check: this plan does not implement login, Client API, Kotlin SDK network calls, Android capabilities, integrations, persistence, sync, or runtime/provider changes.
- Type/interface consistency: architecture guard stable function names and CLI modes are defined once and reused consistently by tests/workflow.
- First functional frontier remains separate and cannot alter its own governance.
