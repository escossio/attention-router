# Repository Guardrails — Architectural Design

Status: **approved design, not implemented**  
Date: 2026-09-11  
Repository: `escossio/attention-router`  
Planning branch: `docs/andy-android-foundation-design-20260911`

This document defines the repository-governance architecture approved before agentic implementation of the Client API bootstrap work. It is a design artifact only. It does **not** claim that guardrail scripts, workflows, scoped `AGENTS.md` files, frontier manifests, protected branch rules, or Client API implementation files are already installed.

The purpose is to turn architecture from informal guidance into executable repository constraints so that Codex or any other coding agent cannot complete a feature as valid while silently placing files in the wrong area, crossing architectural boundaries, expanding scope, or changing the rules that validate its own work.

## 1. Scope

The guardrail system applies initially only to **newly governed areas** created for the Client API/mobile platform work. Existing legacy areas of the repository are not reorganized or retroactively linted unless a future reviewed frontier explicitly includes them.

This is a deliberate compatibility rule: new work gets strong architecture governance without destabilizing working legacy code.

The first governed implementation frontier is named:

`client-api-bootstrap-v1`

## 2. Core governance principles

The following rules are mandatory:

1. **Structural violations are blocking.** A guardrail violation makes CI fail; it is not a warning-only lint.
2. **Feature work uses an explicit allowlist.** Files outside the active frontier's approved paths are rejected.
3. **The candidate PR cannot weaken its own judge.** Governance files and the checker used to validate a feature PR are trusted from the default branch, not from the candidate branch.
4. **Governance changes are separate changes.** If a frontier must expand, the governance is reviewed and changed first; only then may feature work continue.
5. **Architecture is checked independently from behavior.** Passing unit tests does not compensate for violating repository architecture.
6. **Legacy remains outside the initial blast radius.** Existing repository structure is preserved unless a future frontier explicitly governs it.
7. **Fail closed.** Unknown, ambiguous, or unsupported frontier state fails validation rather than permitting the change.
8. **Frontier selection is trusted.** A feature PR cannot choose its frontier through a candidate-controlled file, label, request body, or manifest edit.

## 3. Repository shape for newly governed Client API work

The intended new structure is:

```text
contracts/
└── client/
    └── v1/
        ├── AGENTS.md
        ├── client-api.openapi.json
        └── conformance-cases.json

attention_router/
└── core/
    └── client/
        ├── AGENTS.md
        ├── __init__.py
        └── authority.py

tests/
└── client/
    ├── AGENTS.md
    ├── test_contract.py
    ├── test_conformance.py
    └── test_authority.py

docs/
└── architecture/
    └── client-api/
        └── bootstrap-v1.md

scripts/
├── architecture/
│   └── check_guardrails.py
└── client_contract/
    └── check.py

.github/
└── architecture/
    ├── README.md
    └── frontiers/
        └── client-api-bootstrap-v1.json
```

The new `attention_router/core/client/` package creates a narrow physical boundary for the new client authority model without reorganizing the existing flat `attention_router/core/` legacy modules.

The new `tests/client/` directory gives Client API work its own governed test namespace without moving existing tests.

## 4. Scoped agent instructions

New governed zones contain local `AGENTS.md` files. These files are architectural instructions, not feature implementation files.

### 4.1 `contracts/client/v1/AGENTS.md`

This scope permits only language-neutral public Client API contract artifacts and synthetic conformance fixtures.

It forbids:

- runtime Python logic;
- FastAPI implementation;
- database/persistence code;
- provider-specific transport internals;
- real credentials or personal data;
- local infrastructure addresses or hostnames;
- repurposing `contracts/integration/**` for human-client/session semantics.

### 4.2 `attention_router/core/client/AGENTS.md`

This scope permits pure client authority/domain logic.

It forbids dependencies on:

- FastAPI;
- SQLAlchemy;
- psycopg;
- HTTP clients;
- provider adapters;
- web routes;
- infrastructure repositories;
- Google-specific implementation;
- Android-specific implementation;
- WhatsApp-specific implementation.

### 4.3 `tests/client/AGENTS.md`

This scope contains tests for the governed Client API frontier. Tests must use synthetic identities, tokens, codes, tenants, devices, and payloads.

Real provider accounts, messages, locations, credentials, or runtime inventories are forbidden.

## 5. Trusted governance boundary

Feature pull requests must not be allowed to change the logic that validates themselves.

The trusted governance surface is:

```text
.github/architecture/**
scripts/architecture/**
.github/workflows/architecture-guard.yml
root and scoped governance AGENTS.md files
```

For a normal feature frontier, these paths are outside the feature allowlist.

If a candidate feature PR changes any trusted governance path, validation fails with:

`ARCH_GOVERNANCE_MUTATION`

The architecture checker and frontier policy used to judge the candidate must come from the trusted base/default branch state.

The candidate branch is treated as inspected data. The guard must not import or execute arbitrary Python from the candidate branch while deciding whether it is structurally valid.

No repository secret is required by this guard.

## 6. Frontier manifest model

Each implementation frontier has one manifest under:

`.github/architecture/frontiers/<frontier-id>.json`

The manifest is machine-readable and versioned in Git.

The first manifest is:

`.github/architecture/frontiers/client-api-bootstrap-v1.json`

It records at least:

- `frontier_id`;
- manifest schema version;
- exact approved feature branch name;
- human-readable purpose;
- allowed create/modify paths;
- explicitly forbidden paths/prefixes;
- trusted governance paths that must not change;
- required artifacts;
- file-type restrictions by area;
- Python import/dependency restrictions for governed code;
- documentation/reference restrictions where applicable;
- expected architectural checks;
- the approved spec/plan paths that define the frontier.

The manifest is not self-editable by a feature PR.

### 6.1 Trusted frontier resolution

The guard resolves the active frontier from the **trusted base-branch manifests plus the exact pull-request head branch name**. Candidate content does not select the frontier.

For the first feature frontier, the approved branch name is exactly:

`feat/client-api-bootstrap-v1`

The governance change that installs the frontier writes this exact assignment into the trusted manifest. After that governance change is merged, the feature branch is created from the updated trusted `main` and handed to Codex.

Resolution rules are fail-closed:

- zero trusted manifests match the head branch -> `ARCH_FRONTIER_UNKNOWN`;
- more than one trusted manifest matches -> `ARCH_POLICY_INVALID`;
- candidate attempts to alter branch-to-frontier policy -> `ARCH_GOVERNANCE_MUTATION`;
- candidate-controlled labels, PR body text, files, or workflow inputs do not grant a different frontier.

This model supports future parallel frontiers by giving each one a unique exact approved feature branch in trusted governance.

## 7. First frontier: `client-api-bootstrap-v1`

### 7.1 Allowed implementation paths

The Codex implementation for this frontier may create or modify only:

```text
contracts/client/v1/client-api.openapi.json
contracts/client/v1/conformance-cases.json

attention_router/core/client/__init__.py
attention_router/core/client/authority.py

tests/client/test_contract.py
tests/client/test_conformance.py
tests/client/test_authority.py

scripts/client_contract/check.py

docs/architecture/client-api/bootstrap-v1.md
```

No broad wildcard such as `attention_router/**` is authorized.

### 7.2 Explicitly forbidden areas

The feature frontier must reject changes to at least:

```text
contracts/integration/**
attention_router/infrastructure/**
attention_router/integrations/**
attention_router/adapters/**
attention_router/api/**
alembic/**
whatsapp-transport-local/**
.github/architecture/**
scripts/architecture/**
.github/workflows/**
AGENTS.md
**/AGENTS.md
```

The explicit denylist supplements the narrow allowlist and documents critical architectural boundaries.

### 7.3 Required implementation artifacts

The frontier is incomplete unless these artifacts exist in their approved paths:

```text
contracts/client/v1/client-api.openapi.json
contracts/client/v1/conformance-cases.json
attention_router/core/client/authority.py
tests/client/test_contract.py
tests/client/test_conformance.py
tests/client/test_authority.py
scripts/client_contract/check.py
docs/architecture/client-api/bootstrap-v1.md
```

Presence is a structural condition only; normal tests/checkers validate behavior and content.

## 8. Dependency boundary for `attention_router/core/client/**`

The new client core must remain pure domain/authority code.

Allowed dependencies are primarily:

- Python standard library;
- `dataclasses`;
- `enum`;
- `typing`;
- explicitly approved pure modules from `attention_router.core` where needed.

Forbidden imports include at least:

```text
fastapi
sqlalchemy
psycopg
httpx
requests
pydantic_settings
attention_router.api
attention_router.infrastructure
attention_router.integrations
attention_router.adapters
attention_router.web
```

Provider/platform-specific implementation imports such as Google, Android, or WhatsApp libraries are not valid dependencies of this core package.

The guard should use static inspection such as Python AST parsing. It must not import the candidate module to decide validity.

A forbidden dependency fails with:

`ARCH_DEPENDENCY_FORBIDDEN`

## 9. Contract boundary rules

The Client API contract area is distinct from the existing Integration API contract area.

For `client-api-bootstrap-v1`:

- `contracts/client/**` may be created within the approved files;
- `contracts/integration/**` must remain unchanged;
- Client API human/device/session semantics must not be added to integration contract schemas to avoid the new boundary;
- contract artifacts must remain language-neutral;
- provider-specific runtime implementation details must not leak into canonical client identity/tenant/session semantics.

Crossing the protected contract boundary fails with:

`ARCH_BOUNDARY_CROSSING`

## 10. Path validation

For each pull request, the guard computes the changed paths between the trusted base SHA and candidate SHA.

Every changed path must match the active frontier's allowed set unless it is part of a separately approved governance-only change.

A path outside the active frontier fails with:

`ARCH_PATH_NOT_ALLOWED`

The error must identify:

- the offending path;
- the active frontier;
- the relevant allowed scope;
- the next action: move the responsibility into the approved boundary or request frontier expansion.

The guard must treat symlinks or equivalent path-indirection tricks as invalid for governed implementation paths unless a future reviewed policy explicitly supports them.

## 11. Required-artifact validation

The guard verifies that required frontier artifacts exist in the candidate tree after applying the pull request.

Missing artifacts fail with:

`ARCH_REQUIRED_ARTIFACT_MISSING`

The guard does not treat an empty file as proof of functional correctness; normal contract/tests remain responsible for semantics.

## 12. Frontier expansion protocol

If implementation legitimately requires a path or dependency outside the active manifest, the agent must stop instead of altering the manifest or writing outside the allowlist.

The required handoff status is:

`FRONTIER_EXPANSION_REQUIRED`

The report must include:

- requested file/path;
- reason the current frontier is insufficient;
- required interface/dependency;
- architectural impact;
- test impact;
- whether existing runtime behavior would be affected.

If accepted, governance is updated in a separate reviewed change first. Feature work resumes only after the trusted base contains the expanded policy.

## 13. Architecture Guard workflow

A dedicated GitHub Actions workflow named `architecture-guard.yml` validates governed pull requests.

The base design uses a **base-trusted GitHub Actions execution**, such as `pull_request_target`, because the workflow and checker used to judge a candidate must come from the base/default branch rather than from candidate-controlled workflow content.

Required properties:

- blocking CI result;
- `contents: read` and otherwise minimal permissions;
- no repository/application secrets;
- never checks out and executes candidate code;
- obtains the trusted guard/checker and manifest from the default/base branch state;
- inspects candidate changed paths and blobs through Git/GitHub data access only;
- statically parses candidate Python where dependency validation is needed;
- emits deterministic machine-readable error codes and useful human-readable context;
- fails closed if the frontier cannot be resolved or the policy is malformed.

If `pull_request_target` is used, its elevated-context risks are controlled by the explicit no-secrets/minimal-permissions rule and the prohibition on executing candidate code. An equivalent future mechanism is acceptable only if tests prove that candidate workflow/checker edits cannot affect the judge used for that same PR.

The workflow does not replace the existing Public CI or CodeQL. It adds a separate architecture gate.

## 14. Governance-only pull requests

Normal feature frontiers are not allowed to mutate governance, but governance itself must still be maintainable.

A governance-only branch uses the trusted branch prefix:

`chore/architecture-governance/`

In governance mode, the base-trusted guard enforces a different narrow scope:

- governance files/checker/workflow/scoped `AGENTS.md` may change;
- feature implementation artifacts for Client API, runtime, database, providers, Android, WhatsApp, or deployment may not be added in that same PR;
- guardrail tests/documentation required by the governance plan must accompany behavioral policy changes;
- the change requires human review before merge.

A feature branch that tries to modify trusted governance still receives `ARCH_GOVERNANCE_MUTATION`. Naming a feature branch with the governance prefix does not authorize feature implementation because governance mode itself rejects feature-code paths.

This separation prevents one PR from both changing the fence and placing feature code behind the newly weakened fence.

## 15. Error taxonomy

The first guard implementation must use stable error codes at least for:

- `ARCH_GOVERNANCE_MUTATION` — candidate feature PR modifies trusted governance;
- `ARCH_PATH_NOT_ALLOWED` — candidate changes a path outside the frontier;
- `ARCH_DEPENDENCY_FORBIDDEN` — governed code imports a forbidden dependency;
- `ARCH_BOUNDARY_CROSSING` — candidate crosses a protected architectural area such as Integration API contracts;
- `ARCH_REQUIRED_ARTIFACT_MISSING` — required frontier deliverable is absent;
- `ARCH_FRONTIER_UNKNOWN` — no trusted frontier is assigned to the exact feature branch;
- `ARCH_POLICY_INVALID` — trusted manifest/check policy is malformed, ambiguous, or internally inconsistent.

The CI log must report the error code, offending object/path, active frontier when known, and remediation direction rather than only returning a generic exit code.

## 16. Governance-only installation change

Guardrails are installed before any Codex implementation of the Client API feature.

The first implementation change is therefore a **governance-only PR**, on a branch under:

`chore/architecture-governance/`

That governance PR creates/updates the trusted governance artifacts, the exact branch assignment for `feat/client-api-bootstrap-v1`, and the empty/new-zone structural instruction files required to establish the boundaries.

It must not implement:

- Client API OpenAPI operations;
- client authority behavior;
- FastAPI endpoints;
- database persistence;
- Google authentication;
- email delivery;
- device cryptographic verification;
- Android code;
- WhatsApp changes;
- production deployment.

This separation ensures the fence is installed and reviewed before feature code is placed inside it.

## 17. Revised Client API plan requirement

The existing Client API implementation plan predates the final guardrail paths and must not be handed to Codex unchanged.

Before feature execution, the plan must be revised so that, at minimum:

- `attention_router/core/client_authority.py` becomes `attention_router/core/client/authority.py`;
- Client API tests use `tests/client/**`;
- the standalone Client contract checker uses `scripts/client_contract/check.py`;
- Client API public status documentation uses `docs/architecture/client-api/bootstrap-v1.md`;
- the plan declares frontier `client-api-bootstrap-v1` and exact implementation branch `feat/client-api-bootstrap-v1`;
- no task instructs the feature implementer to edit `.github/architecture/**`, `scripts/architecture/**`, workflow guard files, or scoped governance `AGENTS.md` files.

The revised plan and the manifest must describe the same file boundaries.

## 18. Agent execution handoff

Only after the governance PR is merged into the trusted default branch may Codex begin feature implementation.

The exact approved feature branch `feat/client-api-bootstrap-v1` is then created from that updated trusted base and used for the isolated Codex worktree.

The agent receives:

1. approved Andy Android foundation spec;
2. approved/revised Client API implementation plan;
3. active trusted frontier manifest;
4. applicable root/scoped `AGENTS.md` rules.

The execution instruction requires:

- isolated worktree on the exact approved feature branch from the trusted updated base;
- task-by-task TDD;
- small commits;
- no deployment;
- no merge;
- no live runtime mutation;
- no governance edits;
- no scope expansion outside the frontier;
- stop and report `FRONTIER_EXPANSION_REQUIRED` when needed.

## 19. Merge gates for governed feature work

A governed feature PR is not a merge candidate unless all applicable gates pass:

1. Architecture Guard;
2. Client contract/conformance checker;
3. unit/static tests such as pytest/Ruff/compile checks;
4. existing Public CI;
5. CodeQL/security checks where applicable;
6. human/architectural review against the approved plan/spec.

Behavioral test success never overrides an Architecture Guard failure.

## 20. Test strategy for the guardrail system

The guardrail implementation itself requires synthetic tests that prove both acceptance and rejection behavior.

At minimum test:

- exact approved feature branch resolves the expected frontier;
- unassigned branch fails `ARCH_FRONTIER_UNKNOWN`;
- ambiguous/malformed trusted assignments fail `ARCH_POLICY_INVALID`;
- allowed path accepted;
- unrelated legacy file change rejected for this frontier;
- protected governance file change rejected from a feature frontier;
- governance-mode PR rejects feature implementation paths;
- Integration API contract change rejected;
- forbidden Python import rejected;
- allowed standard-library import accepted;
- missing required artifact rejected;
- malformed manifest rejected fail-closed;
- symlink/path indirection rejected;
- clear stable error codes emitted;
- candidate cannot make itself pass by editing its checker/manifest/workflow in the same feature change.

Tests use synthetic temporary trees/diffs; they do not need live provider access or production secrets.

## 21. Non-goals

This design does not:

- reorganize the entire Attention Router repository;
- make all legacy files comply with the new structure;
- replace code review;
- replace existing tests, Public CI, CodeQL, or secret scanning;
- implement the Client API feature;
- create Android/iOS application code;
- modify live runtime, database, containers, WhatsApp transport, provider accounts, or deployment;
- allow feature PRs to self-authorize frontier expansion.

## 22. Future extension

The frontier model is intended to be reused for subsequent sub-projects, for example:

- `client-identity-persistence-v1`;
- `device-session-v1`;
- `kotlin-sdk-bootstrap-v1`;
- later Android repository frontiers for app shell, capabilities, sync, and integrations.

Each frontier gets its own reviewed territory and exact trusted feature-branch assignment. A future `andy-android` repository should adopt the same principle from its first commit, with stronger layer-specific guards because it will not have the same legacy compatibility constraint.

## 23. Acceptance summary

The approved governance model is:

`approved architecture -> explicit trusted frontier -> repository guardrails -> blocking CI -> isolated agent execution`

For the first frontier:

- governance applies to new Client API/mobile areas only;
- structural violations are blocking;
- implementation uses an exact path allowlist;
- feature PRs cannot mutate their own governance;
- frontier selection comes from trusted base manifests plus an exact approved feature branch;
- the trusted checker/policy comes from the base/default branch;
- `contracts/client` remains separate from `contracts/integration`;
- new pure authority code lives under `attention_router/core/client/`;
- tests live under `tests/client/`;
- legitimate scope expansion requires a separate governance change first;
- governance PRs and feature PRs cannot be combined;
- guardrails are merged before Codex executes the Client API feature plan.

No guardrail implementation starts from this document until this written specification is reviewed and explicitly approved for planning.
