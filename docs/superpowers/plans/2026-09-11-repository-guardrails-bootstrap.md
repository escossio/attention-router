# Repository Guardrails Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install a base-trusted, blocking Architecture Guard for newly governed Client API/mobile areas, publish the first `client-api-bootstrap-v1` frontier, and create scoped repository instructions before any Client API feature implementation begins.

**Architecture:** The guard runs from trusted default-branch code and treats pull-request candidate content only as data. It first decides whether guardrails apply, then resolves either governance mode or an exact trusted feature frontier, validates changed paths and required artifacts, statically inspects governed Python imports without importing candidate code, and fails with stable error codes. Unassigned pull requests that touch only legacy/unmanaged areas pass as `ARCH_NOT_APPLICABLE`; unassigned pull requests that touch governed territory fail closed.

**Tech Stack:** Python 3.11+, Python `ast`, Git plumbing (`git diff`, `git ls-tree`, `git show`, `git cat-file`), JSON frontier manifests, pytest, GitHub Actions `pull_request_target`, existing Public CI/CodeQL, GitHub repository ruleset `main-public-ci-protection`.

**Spec:** `docs/superpowers/specs/2026-09-11-repository-guardrails-design.md`  
**Applicability amendment:** `docs/superpowers/specs/2026-09-11-repository-guardrails-applicability-amendment.md`

## Global Constraints

- Structural guardrail violations are blocking, not warning-only.
- Guardrails initially govern only newly created Client API/mobile areas; unrelated legacy-only pull requests remain outside the initial blast radius.
- Feature work uses an explicit allowlist; no broad `attention_router/**` permission is allowed.
- Candidate pull requests cannot weaken the checker, manifest, workflow, or scoped governance instructions used to judge themselves.
- Trusted checker and frontier policy come from the base/default branch, never candidate-controlled content.
- Candidate Python is inspected statically and is never imported or executed by the Architecture Guard.
- No repository/application secrets are supplied to the Architecture Guard.
- Unknown/unassigned branches touching governed territory fail closed; unknown branches touching only legacy/unmanaged paths succeed as `ARCH_NOT_APPLICABLE`.
- Governance changes use the prefix `chore/architecture-governance/` and cannot include Client API feature implementation.
- The first approved feature branch is exactly `feat/client-api-bootstrap-v1`.
- The first feature frontier is exactly `client-api-bootstrap-v1`.
- `contracts/client/**` remains separate from `contracts/integration/**`.
- New pure client authority code belongs under `attention_router/core/client/`.
- New Client API tests belong under `tests/client/`.
- No FastAPI endpoint, database migration, Google call, email delivery, Android code, WhatsApp change, runtime mutation, provider activation, or deployment is part of this plan.
- The first governance installation PR is a one-time bootstrap exception because the base branch does not yet contain the guard; it must pass existing Public CI/CodeQL/secret scanning plus guardrail tests and human review before merge.
- After the bootstrap governance PR is merged, later governance and governed-feature PRs must be evaluated by the base-trusted Architecture Guard.

---

## File Structure

### Governance files created or modified by this plan

- Modify `AGENTS.md` — root automation instructions point agents to active frontier/scoped rules and prohibit self-expansion of governance.
- Create `contracts/client/v1/AGENTS.md` — language-neutral Client API contract zone rules.
- Create `attention_router/core/client/AGENTS.md` — pure client authority/core rules.
- Create `tests/client/AGENTS.md` — synthetic Client API test-zone rules.
- Create `.github/architecture/README.md` — human-readable architecture-governance contract and frontier expansion protocol.
- Create `.github/architecture/frontiers/client-api-bootstrap-v1.json` — trusted machine-readable first frontier.
- Create `scripts/architecture/check_guardrails.py` — base-trusted static guard implementation and CLI.
- Create `tests/architecture/test_guardrails_policy.py` — unit tests for manifest validation, applicability, frontier resolution, path rules, import rules, error codes.
- Create `tests/architecture/test_guardrails_git_integration.py` — temporary-Git integration tests for candidate diffs, blobs, required artifacts, symlinks, and self-mutation attempts.
- Create `.github/workflows/architecture-guard.yml` — base-trusted read-only GitHub Actions gate.

### Files explicitly not created by this plan

The following feature artifacts are reserved for the later Client API implementation frontier and must not appear in the governance PR:

- `contracts/client/v1/client-api.openapi.json`
- `contracts/client/v1/conformance-cases.json`
- `attention_router/core/client/__init__.py`
- `attention_router/core/client/authority.py`
- `tests/client/test_contract.py`
- `tests/client/test_conformance.py`
- `tests/client/test_authority.py`
- `scripts/client_contract/check.py`
- `docs/architecture/client-api/bootstrap-v1.md`

### Execution branch

The implementation branch for this plan must be:

`chore/architecture-governance/client-frontier-v1`

It must be created from the then-current trusted `main` after the planning/spec documentation is present on `main`.

---

### Task 1: Create the repository governance skeleton and scoped agent instructions

**Files:**
- Modify: `AGENTS.md`
- Create: `.github/architecture/README.md`
- Create: `contracts/client/v1/AGENTS.md`
- Create: `attention_router/core/client/AGENTS.md`
- Create: `tests/client/AGENTS.md`

**Interfaces:**
- Consumes: repository guardrail design and applicability amendment.
- Produces: human/agent instructions used by later tasks and by Codex before feature implementation.

- [ ] **Step 1: Write tests that assert the governance instruction files exist and contain the critical stop rules**

Create `tests/architecture/test_guardrails_policy.py` initially with:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_scoped_agent_instructions_define_the_new_boundaries():
    contract_rules = read("contracts/client/v1/AGENTS.md")
    core_rules = read("attention_router/core/client/AGENTS.md")
    test_rules = read("tests/client/AGENTS.md")

    assert "language-neutral" in contract_rules
    assert "contracts/integration" in contract_rules
    assert "FastAPI" in core_rules
    assert "SQLAlchemy" in core_rules
    assert "synthetic" in test_rules.lower()


def test_root_agent_rules_require_frontier_stop_on_scope_expansion():
    root_rules = read("AGENTS.md")
    assert "FRONTIER_EXPANSION_REQUIRED" in root_rules
    assert ".github/architecture" in root_rules
```

- [ ] **Step 2: Run the focused test and verify it fails because the new governance files do not exist**

Run:

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: FAIL on missing scoped `AGENTS.md` files and/or missing root frontier text.

- [ ] **Step 3: Update the root `AGENTS.md` without removing existing repository safety rules**

Keep the existing text and append this exact governance section:

```markdown
## Governed frontiers

Before changing a newly governed area, read `.github/architecture/README.md`, the applicable trusted frontier manifest under `.github/architecture/frontiers/`, and every scoped `AGENTS.md` covering the target path.

Do not change `.github/architecture/**`, `scripts/architecture/**`, `.github/workflows/architecture-guard.yml`, or scoped governance `AGENTS.md` from a normal feature frontier.

If an approved task requires a path or dependency outside its frontier, stop and report `FRONTIER_EXPANSION_REQUIRED` with the requested path, reason, interface/dependency, architectural impact, test impact, and runtime impact. Do not widen the frontier from the feature branch.
```

- [ ] **Step 4: Create `.github/architecture/README.md`**

Use this content:

```markdown
# Repository Architecture Guardrails

This directory contains trusted repository-governance policy. It is not feature implementation.

A normal governed feature must use an exact trusted frontier declared under `frontiers/`. The candidate branch cannot select, edit, or widen the policy that validates itself.

The Architecture Guard reads policy from the trusted base/default branch and treats candidate pull-request content as data only. Structural violations fail CI.

Unassigned pull requests that touch only legacy/unmanaged areas are outside the current guardrail scope and pass as `ARCH_NOT_APPLICABLE`. An unassigned pull request that touches governed territory fails closed.

Governance-only changes use branches under `chore/architecture-governance/` and may change only governance/checker/workflow/test/instruction paths allowed by governance mode. Governance and feature implementation must not be combined in one pull request.

A feature that needs to leave its approved frontier must stop with `FRONTIER_EXPANSION_REQUIRED`. Governance is reviewed and merged first; feature work resumes from the updated trusted base afterward.
```

- [ ] **Step 5: Create `contracts/client/v1/AGENTS.md`**

```markdown
# Client API contract zone

This directory contains language-neutral public Client API contracts and synthetic conformance fixtures only.

Allowed responsibilities: OpenAPI/JSON contract artifacts, schema-compatible synthetic fixtures, and contract-local explanatory metadata approved by the active frontier.

Forbidden responsibilities: runtime Python, FastAPI routes, database/persistence code, provider transport internals, real credentials, personal data, local infrastructure addresses/hostnames, or human-client/session semantics placed in `contracts/integration/**`.

A Client API feature may modify only paths explicitly listed by its trusted frontier. If another path is required, stop with `FRONTIER_EXPANSION_REQUIRED`.
```

- [ ] **Step 6: Create `attention_router/core/client/AGENTS.md`**

```markdown
# Client authority core zone

This package is for pure client identity/tenant/device/session authority logic. Keep it deterministic and infrastructure-independent.

Do not depend on FastAPI, SQLAlchemy, psycopg, HTTP clients, web routes, infrastructure repositories, adapters, provider integrations, Google-specific implementation, Android-specific implementation, or WhatsApp-specific implementation.

Candidate code is expected to remain testable with Python standard-library inputs and synthetic domain data. Do not add runtime I/O here.

A feature may modify only paths explicitly listed by its trusted frontier. If another dependency or path is required, stop with `FRONTIER_EXPANSION_REQUIRED`.
```

- [ ] **Step 7: Create `tests/client/AGENTS.md`**

```markdown
# Client API test zone

This directory contains tests for governed Client API/mobile platform frontiers.

Use synthetic identities, tenant IDs, device IDs, tokens, verification codes, payloads, and timestamps. Never commit real provider accounts, conversations, precise locations, credentials, secrets, or runtime/environment inventories.

Keep tests scoped to the active frontier. If a test requires modifying production code outside the approved frontier, stop with `FRONTIER_EXPANSION_REQUIRED` instead of expanding scope implicitly.
```

- [ ] **Step 8: Run the focused governance instruction test**

Run:

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: PASS for the instruction-file tests added in Step 1.

- [ ] **Step 9: Commit the governance skeleton**

```bash
git add AGENTS.md .github/architecture/README.md \
  contracts/client/v1/AGENTS.md \
  attention_router/core/client/AGENTS.md \
  tests/client/AGENTS.md \
  tests/architecture/test_guardrails_policy.py
git commit -m "chore: establish repository guardrail zones"
```

---

### Task 2: Define and validate the trusted first frontier manifest

**Files:**
- Create: `.github/architecture/frontiers/client-api-bootstrap-v1.json`
- Modify: `tests/architecture/test_guardrails_policy.py`
- Create: `scripts/architecture/check_guardrails.py`

**Interfaces:**
- Consumes: scoped governance instructions from Task 1.
- Produces: `FrontierPolicy`, manifest loader/validator, trusted branch assignment, governed-root metadata, stable error model used by Tasks 3–6.

- [ ] **Step 1: Add failing manifest-validation tests**

Append to `tests/architecture/test_guardrails_policy.py`:

```python
import json

import pytest

from scripts.architecture.check_guardrails import (
    ARCH_POLICY_INVALID,
    GuardViolation,
    load_frontier_policies,
)


def test_first_frontier_manifest_has_exact_branch_and_exact_feature_paths():
    policies = load_frontier_policies(ROOT)
    policy = policies["client-api-bootstrap-v1"]

    assert policy.approved_head_ref == "feat/client-api-bootstrap-v1"
    assert set(policy.allowed_paths) == {
        "contracts/client/v1/client-api.openapi.json",
        "contracts/client/v1/conformance-cases.json",
        "attention_router/core/client/__init__.py",
        "attention_router/core/client/authority.py",
        "tests/client/test_contract.py",
        "tests/client/test_conformance.py",
        "tests/client/test_authority.py",
        "scripts/client_contract/check.py",
        "docs/architecture/client-api/bootstrap-v1.md",
    }


def test_duplicate_frontier_branch_assignment_is_policy_invalid(tmp_path):
    root = tmp_path
    frontier_dir = root / ".github/architecture/frontiers"
    frontier_dir.mkdir(parents=True)
    source = json.loads(
        (ROOT / ".github/architecture/frontiers/client-api-bootstrap-v1.json").read_text()
    )
    (frontier_dir / "one.json").write_text(json.dumps(source))
    source["frontier_id"] = "duplicate"
    (frontier_dir / "two.json").write_text(json.dumps(source))

    with pytest.raises(GuardViolation) as exc:
        load_frontier_policies(root)
    assert exc.value.code == ARCH_POLICY_INVALID
```

- [ ] **Step 2: Run the focused tests and verify they fail because the checker/manifest do not exist**

Run:

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: FAIL importing `scripts.architecture.check_guardrails` or loading the manifest.

- [ ] **Step 3: Create the checker foundation with stable public constants and dataclasses**

Create `scripts/architecture/check_guardrails.py` with this initial API:

```python
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ARCH_GOVERNANCE_MUTATION = "ARCH_GOVERNANCE_MUTATION"
ARCH_PATH_NOT_ALLOWED = "ARCH_PATH_NOT_ALLOWED"
ARCH_DEPENDENCY_FORBIDDEN = "ARCH_DEPENDENCY_FORBIDDEN"
ARCH_BOUNDARY_CROSSING = "ARCH_BOUNDARY_CROSSING"
ARCH_REQUIRED_ARTIFACT_MISSING = "ARCH_REQUIRED_ARTIFACT_MISSING"
ARCH_FRONTIER_UNKNOWN = "ARCH_FRONTIER_UNKNOWN"
ARCH_POLICY_INVALID = "ARCH_POLICY_INVALID"
ARCH_CANDIDATE_INVALID = "ARCH_CANDIDATE_INVALID"
ARCH_CONTENT_FORBIDDEN = "ARCH_CONTENT_FORBIDDEN"
ARCH_NOT_APPLICABLE = "ARCH_NOT_APPLICABLE"
ARCH_PASS = "ARCH_PASS"


@dataclass(frozen=True)
class GuardViolation(Exception):
    code: str
    subject: str
    detail: str
    frontier_id: str | None = None

    def __str__(self) -> str:
        scope = f" frontier={self.frontier_id}" if self.frontier_id else ""
        return f"{self.code}:{scope} subject={self.subject} detail={self.detail}"


@dataclass(frozen=True)
class FrontierPolicy:
    schema_version: str
    frontier_id: str
    approved_head_ref: str
    purpose: str
    allowed_paths: tuple[str, ...]
    forbidden_prefixes: tuple[str, ...]
    trusted_governance_paths: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    governed_roots: tuple[str, ...]
    forbidden_python_imports: tuple[str, ...]
    allowed_internal_imports: tuple[str, ...]
    forbidden_contract_text: tuple[str, ...]
    spec_paths: tuple[str, ...]
    plan_path: str
```

Implement `load_frontier_policies(root: Path) -> dict[str, FrontierPolicy]` so it:

- reads only `root/.github/architecture/frontiers/*.json`;
- requires every field above;
- requires `schema_version == "1"`;
- rejects blank/duplicate `frontier_id` values;
- rejects blank/duplicate `approved_head_ref` assignments;
- rejects duplicate allowed paths;
- rejects any allowed path that is also inside a trusted-governance path;
- requires relative normalized POSIX-style repository paths only;
- raises `GuardViolation(ARCH_POLICY_INVALID, ...)` on every malformed policy condition.

- [ ] **Step 4: Create the exact first frontier manifest**

Create `.github/architecture/frontiers/client-api-bootstrap-v1.json`:

```json
{
  "schema_version": "1",
  "frontier_id": "client-api-bootstrap-v1",
  "approved_head_ref": "feat/client-api-bootstrap-v1",
  "purpose": "First language-neutral Client API bootstrap contract and pure authority model",
  "allowed_paths": [
    "contracts/client/v1/client-api.openapi.json",
    "contracts/client/v1/conformance-cases.json",
    "attention_router/core/client/__init__.py",
    "attention_router/core/client/authority.py",
    "tests/client/test_contract.py",
    "tests/client/test_conformance.py",
    "tests/client/test_authority.py",
    "scripts/client_contract/check.py",
    "docs/architecture/client-api/bootstrap-v1.md"
  ],
  "forbidden_prefixes": [
    "contracts/integration/",
    "attention_router/infrastructure/",
    "attention_router/integrations/",
    "attention_router/adapters/",
    "attention_router/api/",
    "alembic/",
    "whatsapp-transport-local/"
  ],
  "trusted_governance_paths": [
    ".github/architecture/",
    "scripts/architecture/",
    ".github/workflows/architecture-guard.yml",
    "AGENTS.md",
    "contracts/client/v1/AGENTS.md",
    "attention_router/core/client/AGENTS.md",
    "tests/client/AGENTS.md"
  ],
  "required_artifacts": [
    "contracts/client/v1/client-api.openapi.json",
    "contracts/client/v1/conformance-cases.json",
    "attention_router/core/client/authority.py",
    "tests/client/test_contract.py",
    "tests/client/test_conformance.py",
    "tests/client/test_authority.py",
    "scripts/client_contract/check.py",
    "docs/architecture/client-api/bootstrap-v1.md"
  ],
  "governed_roots": [
    "contracts/client/",
    "attention_router/core/client/",
    "tests/client/",
    "scripts/client_contract/",
    "docs/architecture/client-api/"
  ],
  "forbidden_python_imports": [
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "httpx",
    "requests",
    "pydantic_settings",
    "attention_router.api",
    "attention_router.infrastructure",
    "attention_router.integrations",
    "attention_router.adapters",
    "attention_router.web"
  ],
  "allowed_internal_imports": [
    "attention_router.core.client"
  ],
  "forbidden_contract_text": [
    "agt01",
    "192.168.88.",
    "/srv/projetos/",
    "DATABASE_URL",
    "postgresql://"
  ],
  "spec_paths": [
    "docs/superpowers/specs/2026-09-11-andy-android-foundation-design.md",
    "docs/superpowers/specs/2026-09-11-repository-guardrails-design.md",
    "docs/superpowers/specs/2026-09-11-repository-guardrails-applicability-amendment.md"
  ],
  "plan_path": "docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md"
}
```

- [ ] **Step 5: Run the manifest tests**

Run:

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: PASS for manifest loading/uniqueness tests.

- [ ] **Step 6: Commit the trusted frontier manifest and checker foundation**

```bash
git add .github/architecture/frontiers/client-api-bootstrap-v1.json \
  scripts/architecture/check_guardrails.py \
  tests/architecture/test_guardrails_policy.py
git commit -m "feat: define trusted client API frontier"
```

---

### Task 3: Implement applicability, frontier resolution, and path governance

**Files:**
- Modify: `scripts/architecture/check_guardrails.py`
- Modify: `tests/architecture/test_guardrails_policy.py`

**Interfaces:**
- Consumes: `FrontierPolicy` objects from Task 2.
- Produces: deterministic mode resolution and changed-path validation used by the Git integration layer and workflow.

- [ ] **Step 1: Add failing applicability/frontier/path tests**

Append:

```python
from scripts.architecture.check_guardrails import (
    ARCH_FRONTIER_UNKNOWN,
    ARCH_GOVERNANCE_MUTATION,
    ARCH_NOT_APPLICABLE,
    ARCH_PATH_NOT_ALLOWED,
    resolve_mode,
    validate_changed_paths,
)


def first_policy():
    return load_frontier_policies(ROOT)["client-api-bootstrap-v1"]


def test_unassigned_legacy_only_change_is_not_applicable():
    result = resolve_mode(
        head_ref="dependabot/pip/main/legacy-deps",
        changed_paths=("pyproject.toml",),
        policies={"client-api-bootstrap-v1": first_policy()},
    )
    assert result.kind == "not_applicable"
    assert result.code == ARCH_NOT_APPLICABLE


def test_unassigned_branch_touching_governed_root_fails_unknown_frontier():
    with pytest.raises(GuardViolation) as exc:
        resolve_mode(
            head_ref="feat/random-client-work",
            changed_paths=("contracts/client/v1/client-api.openapi.json",),
            policies={"client-api-bootstrap-v1": first_policy()},
        )
    assert exc.value.code == ARCH_FRONTIER_UNKNOWN


def test_unassigned_branch_touching_trusted_governance_fails_mutation():
    with pytest.raises(GuardViolation) as exc:
        resolve_mode(
            head_ref="feat/random-work",
            changed_paths=("scripts/architecture/check_guardrails.py",),
            policies={"client-api-bootstrap-v1": first_policy()},
        )
    assert exc.value.code == ARCH_GOVERNANCE_MUTATION


def test_exact_feature_branch_resolves_frontier():
    result = resolve_mode(
        head_ref="feat/client-api-bootstrap-v1",
        changed_paths=("contracts/client/v1/client-api.openapi.json",),
        policies={"client-api-bootstrap-v1": first_policy()},
    )
    assert result.kind == "feature"
    assert result.frontier_id == "client-api-bootstrap-v1"


def test_feature_frontier_rejects_unrelated_legacy_file_even_if_legacy_is_unmanaged():
    policy = first_policy()
    with pytest.raises(GuardViolation) as exc:
        validate_changed_paths(policy, ("README.md",))
    assert exc.value.code == ARCH_PATH_NOT_ALLOWED
```

- [ ] **Step 2: Run focused tests and verify failure**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: FAIL because resolution/path validation is not implemented.

- [ ] **Step 3: Implement normalized-path and governance-path helpers**

Add pure helpers:

```python
@dataclass(frozen=True)
class GuardMode:
    kind: str
    code: str
    frontier_id: str | None = None


def normalize_repo_path(value: str) -> str:
    path = value.replace("\\", "/")
    if not path or path.startswith("/") or path.startswith("../") or "/../" in f"/{path}":
        raise GuardViolation(ARCH_POLICY_INVALID, value, "path must be normalized and relative")
    while path.startswith("./"):
        path = path[2:]
    if "//" in path:
        raise GuardViolation(ARCH_POLICY_INVALID, value, "path contains duplicate separators")
    return path


def path_matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
```

Implement `trusted_governance_paths(policies)` and `governed_roots(policies)` as union sets derived only from trusted manifests plus fixed bootstrap governance paths.

- [ ] **Step 4: Implement applicability and exact frontier resolution**

Implement this order exactly:

```text
head starts chore/architecture-governance/
  -> governance mode

exactly one trusted policy approved_head_ref == head
  -> feature mode

no exact policy match + changed trusted governance path
  -> ARCH_GOVERNANCE_MUTATION

no exact policy match + changed governed root
  -> ARCH_FRONTIER_UNKNOWN

otherwise
  -> success ARCH_NOT_APPLICABLE
```

If more than one manifest claims the same branch, the manifest loader has already failed `ARCH_POLICY_INVALID`.

- [ ] **Step 5: Implement feature path validation with deterministic error priority**

For feature mode, validate each changed path in this priority:

1. trusted governance path -> `ARCH_GOVERNANCE_MUTATION`;
2. `contracts/integration/**` or another explicit protected prefix -> `ARCH_BOUNDARY_CROSSING`;
3. not in exact `allowed_paths` -> `ARCH_PATH_NOT_ALLOWED`;
4. otherwise accepted for deeper validation.

Do not silently permit deletions. A deletion is a changed path and remains subject to the same rules; required-artifact validation later catches deletion of required files.

- [ ] **Step 6: Implement governance-mode path validation**

Governance mode may change only:

```text
AGENTS.md
.github/architecture/**
scripts/architecture/**
.github/workflows/architecture-guard.yml
tests/architecture/**
contracts/client/v1/AGENTS.md
attention_router/core/client/AGENTS.md
tests/client/AGENTS.md
```

Any other changed path fails `ARCH_PATH_NOT_ALLOWED`. In particular, all nine reserved Client API feature artifact paths listed in the File Structure section must fail governance mode.

- [ ] **Step 7: Run focused tests and commit**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
git add scripts/architecture/check_guardrails.py tests/architecture/test_guardrails_policy.py
git commit -m "feat: enforce frontier applicability and paths"
```

Expected: PASS.

---

### Task 4: Add static import, content, artifact, and symlink checks

**Files:**
- Modify: `scripts/architecture/check_guardrails.py`
- Modify: `tests/architecture/test_guardrails_policy.py`

**Interfaces:**
- Consumes: feature mode/path validation from Task 3 and candidate blob/entry callbacks supplied by Task 5.
- Produces: candidate-content validators that never import/execute candidate code.

- [ ] **Step 1: Add failing import/content/artifact tests with in-memory candidate accessors**

Add tests for these exact cases:

```python
from scripts.architecture.check_guardrails import (
    ARCH_CANDIDATE_INVALID,
    ARCH_CONTENT_FORBIDDEN,
    ARCH_DEPENDENCY_FORBIDDEN,
    ARCH_REQUIRED_ARTIFACT_MISSING,
    validate_candidate_content,
    validate_required_artifacts,
)


def test_forbidden_python_import_is_rejected():
    policy = first_policy()
    blobs = {
        "attention_router/core/client/authority.py": "from sqlalchemy import select\n",
    }
    with pytest.raises(GuardViolation) as exc:
        validate_candidate_content(policy, blobs.get, lambda path: "100644")
    assert exc.value.code == ARCH_DEPENDENCY_FORBIDDEN


def test_stdlib_only_python_is_allowed():
    policy = first_policy()
    blobs = {
        "attention_router/core/client/authority.py": "from dataclasses import dataclass\nfrom enum import StrEnum\n",
    }
    validate_candidate_content(policy, blobs.get, lambda path: "100644")


def test_unapproved_attention_router_internal_import_is_rejected():
    policy = first_policy()
    blobs = {
        "attention_router/core/client/authority.py": "from attention_router.infrastructure.db import session\n",
    }
    with pytest.raises(GuardViolation) as exc:
        validate_candidate_content(policy, blobs.get, lambda path: "100644")
    assert exc.value.code == ARCH_DEPENDENCY_FORBIDDEN


def test_client_contract_rejects_local_infrastructure_reference():
    policy = first_policy()
    blobs = {
        "contracts/client/v1/client-api.openapi.json": '{"description":"call agt01"}',
    }
    with pytest.raises(GuardViolation) as exc:
        validate_candidate_content(policy, blobs.get, lambda path: "100644")
    assert exc.value.code == ARCH_CONTENT_FORBIDDEN


def test_candidate_symlink_is_rejected():
    policy = first_policy()
    blobs = {"attention_router/core/client/authority.py": "../../outside.py"}
    with pytest.raises(GuardViolation) as exc:
        validate_candidate_content(policy, blobs.get, lambda path: "120000")
    assert exc.value.code == ARCH_CANDIDATE_INVALID


def test_missing_required_artifact_is_rejected():
    policy = first_policy()
    existing = set(policy.required_artifacts) - {"tests/client/test_authority.py"}
    with pytest.raises(GuardViolation) as exc:
        validate_required_artifacts(policy, existing.__contains__)
    assert exc.value.code == ARCH_REQUIRED_ARTIFACT_MISSING
```

- [ ] **Step 2: Run tests and verify they fail**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: FAIL because the content/artifact validators do not exist.

- [ ] **Step 3: Implement static Python import extraction**

Use `ast.parse(candidate_text)` and inspect only `ast.Import`/`ast.ImportFrom` nodes. Do not import candidate modules.

Rules for `attention_router/core/client/*.py`:

- standard-library imports are allowed;
- imports beginning with any `forbidden_python_imports` entry fail `ARCH_DEPENDENCY_FORBIDDEN`;
- imports beginning with `attention_router.` are allowed only if they begin with an entry in `allowed_internal_imports`;
- any other third-party module import is rejected as `ARCH_DEPENDENCY_FORBIDDEN` for this first pure-core frontier;
- Python syntax that cannot be parsed fails `ARCH_CANDIDATE_INVALID`.

Use `sys.stdlib_module_names` to recognize Python standard-library top-level modules.

- [ ] **Step 4: Implement candidate entry-mode and forbidden-text validation**

`validate_candidate_content` must:

- reject mode `120000` for any changed or required governed artifact as `ARCH_CANDIDATE_INVALID`;
- reject non-regular modes other than `100644`/`100755` unless future policy explicitly allows them;
- inspect only candidate blobs that are relevant to the active policy;
- scan `contracts/client/v1/*.json` for exact case-insensitive occurrences of manifest `forbidden_contract_text` values;
- emit `ARCH_CONTENT_FORBIDDEN` without printing the entire candidate blob.

- [ ] **Step 5: Implement required-artifact existence validation**

`validate_required_artifacts(policy, exists)` checks the candidate tree after the PR. Each required path must exist and be a regular file. Missing paths fail `ARCH_REQUIRED_ARTIFACT_MISSING` with the missing path as subject.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
git add scripts/architecture/check_guardrails.py tests/architecture/test_guardrails_policy.py
git commit -m "feat: validate governed candidate content"
```

Expected: PASS.

---

### Task 5: Bind the checker to real Git object data without executing candidate code

**Files:**
- Modify: `scripts/architecture/check_guardrails.py`
- Create: `tests/architecture/test_guardrails_git_integration.py`

**Interfaces:**
- Consumes: pure validators from Tasks 2–4.
- Produces: `run_guard(...)`, Git blob/tree accessors, CLI exit semantics used by GitHub Actions.

- [ ] **Step 1: Write a temporary-Git integration fixture**

Create `tests/architecture/test_guardrails_git_integration.py` with helpers that:

- initialize a temporary Git repository;
- configure synthetic author name/email;
- create a base commit containing copied trusted manifest/checker-independent fixtures;
- create candidate commits with exact paths/content;
- return base/head SHAs.

The helper must use subprocess Git commands only against the temporary directory; it must never touch the real repository history.

- [ ] **Step 2: Add failing Git integration cases**

Add tests proving:

```text
unassigned branch + legacy README-only change
  -> PASS / ARCH_NOT_APPLICABLE

feat/client-api-bootstrap-v1 + all required allowed files
  -> PASS / ARCH_PASS

feat/client-api-bootstrap-v1 + README.md
  -> FAIL / ARCH_PATH_NOT_ALLOWED

feat/client-api-bootstrap-v1 + contracts/integration/v1/anything.json
  -> FAIL / ARCH_BOUNDARY_CROSSING

feat/client-api-bootstrap-v1 + scripts/architecture/check_guardrails.py
  -> FAIL / ARCH_GOVERNANCE_MUTATION

feat/client-api-bootstrap-v1 + manifest/checker/workflow mutation together
  -> FAIL / ARCH_GOVERNANCE_MUTATION

feat/client-api-bootstrap-v1 + symlink authority.py
  -> FAIL / ARCH_CANDIDATE_INVALID

feat/random-client + contracts/client/v1/client-api.openapi.json
  -> FAIL / ARCH_FRONTIER_UNKNOWN

chore/architecture-governance/example + only governance paths
  -> PASS / ARCH_PASS

chore/architecture-governance/example + authority.py feature code
  -> FAIL / ARCH_PATH_NOT_ALLOWED
```

- [ ] **Step 3: Run integration tests and verify failure before Git accessors exist**

```bash
python -m pytest -q tests/architecture/test_guardrails_git_integration.py
```

Expected: FAIL.

- [ ] **Step 4: Implement safe Git plumbing wrappers**

Add:

```python
def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
```

Implement:

- `changed_paths(repo, base_sha, head_sha)` using `git diff --name-status -z` and returning normalized candidate paths, including renames as both source and destination changes where needed;
- `candidate_exists(repo, head_sha, path)` using `git cat-file -e <head>:<path>`;
- `candidate_blob(repo, head_sha, path)` using `git show <head>:<path>`;
- `candidate_mode(repo, head_sha, path)` using `git ls-tree <head> -- <path>`.

Never use `git checkout <head_sha>`, never run an executable from the candidate tree, and never add candidate paths to Python import search paths.

- [ ] **Step 5: Implement `run_guard`**

Signature:

```python
def run_guard(
    *,
    repo: Path,
    base_sha: str,
    head_sha: str,
    head_ref: str,
) -> GuardMode:
    ...
```

Order:

1. load trusted policies from the current checked-out trusted base working tree;
2. compute candidate changed paths from Git objects;
3. resolve governance/feature/not-applicable mode;
4. if not applicable, return successful `GuardMode("not_applicable", ARCH_NOT_APPLICABLE)`;
5. governance mode: validate governance allowlist only;
6. feature mode: validate changed paths, required artifacts, candidate modes/blobs, imports, and forbidden contract text;
7. return `GuardMode("pass", ARCH_PASS, frontier_id)`.

Any `GuardViolation` propagates to the CLI and becomes exit code 1.

- [ ] **Step 6: Implement the CLI**

CLI arguments:

```text
--repo PATH              required
--base-sha SHA           required
--head-sha SHA           required
--head-ref BRANCH        required
--json                    optional machine-readable output
```

Successful text output examples:

```text
ARCHITECTURE_GUARD_STATUS=PASS
ARCHITECTURE_GUARD_RESULT=ARCH_NOT_APPLICABLE
```

or:

```text
ARCHITECTURE_GUARD_STATUS=PASS
ARCHITECTURE_GUARD_RESULT=ARCH_PASS
ARCHITECTURE_GUARD_FRONTIER=client-api-bootstrap-v1
```

Failure output must contain at least:

```text
ARCHITECTURE_GUARD_STATUS=FAIL
ARCHITECTURE_GUARD_RESULT=<stable error code>
ARCHITECTURE_GUARD_SUBJECT=<path or object>
ARCHITECTURE_GUARD_FRONTIER=<frontier when known>
ARCHITECTURE_GUARD_DETAIL=<short remediation-oriented detail>
```

Never print candidate secrets/full blobs in an error.

- [ ] **Step 7: Run all guardrail tests and commit**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py \
  tests/architecture/test_guardrails_git_integration.py
git add scripts/architecture/check_guardrails.py \
  tests/architecture/test_guardrails_policy.py \
  tests/architecture/test_guardrails_git_integration.py
git commit -m "feat: inspect pull requests with trusted git guard"
```

Expected: PASS.

---

### Task 6: Install the base-trusted GitHub Actions Architecture Guard

**Files:**
- Create: `.github/workflows/architecture-guard.yml`
- Modify: `tests/architecture/test_guardrails_policy.py`

**Interfaces:**
- Consumes: CLI from Task 5.
- Produces: GitHub status check named exactly `architecture-guard` for PRs targeting `main` after this governance PR is merged.

- [ ] **Step 1: Add a static workflow safety test**

Append to `tests/architecture/test_guardrails_policy.py`:

```python

def test_architecture_guard_workflow_is_base_trusted_and_read_only():
    workflow = read(".github/workflows/architecture-guard.yml")
    assert "pull_request_target:" in workflow
    assert "contents: read" in workflow
    assert "architecture-guard" in workflow
    assert "pull/${PR_NUMBER}/head" in workflow
    assert "check_guardrails.py" in workflow
    assert "secrets." not in workflow
    assert "github.event.pull_request.head.repo" not in workflow
    assert "checkout" in workflow
```

Also assert the workflow does not contain these dangerous patterns:

```python
for forbidden in (
    "ref: ${{ github.event.pull_request.head.sha }}",
    "python ${{",
    "bash ${{",
    "./${{ github.event.pull_request.head",
):
    assert forbidden not in workflow
```

- [ ] **Step 2: Run the workflow test and verify failure because the workflow is absent**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py
```

Expected: FAIL loading `.github/workflows/architecture-guard.yml`.

- [ ] **Step 3: Create `.github/workflows/architecture-guard.yml`**

Use this structure:

```yaml
name: Architecture Guard

on:
  pull_request_target:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read

concurrency:
  group: architecture-guard-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  architecture-guard:
    name: architecture-guard
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout trusted base guard
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          fetch-depth: 1
          persist-credentials: false

      - name: Fetch candidate pull-request objects without checkout
        shell: bash
        env:
          PR_NUMBER: ${{ github.event.pull_request.number }}
          EXPECTED_HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          set -euo pipefail
          git fetch --no-tags --depth=1 origin "pull/${PR_NUMBER}/head:refs/remotes/origin/architecture-guard-candidate"
          ACTUAL_HEAD_SHA="$(git rev-parse refs/remotes/origin/architecture-guard-candidate)"
          test "$ACTUAL_HEAD_SHA" = "$EXPECTED_HEAD_SHA"

      - name: Run trusted Architecture Guard
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          HEAD_REF: ${{ github.event.pull_request.head.ref }}
        run: |
          python scripts/architecture/check_guardrails.py \
            --repo . \
            --base-sha "$BASE_SHA" \
            --head-sha "$HEAD_SHA" \
            --head-ref "$HEAD_REF"
```

Do not add `secrets`, write permissions, candidate checkout, `pip install` from candidate requirements, or candidate-script execution.

- [ ] **Step 4: Run guardrail tests, YAML-sensitive repository checks, and the full offline Python suite**

Run:

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py \
  tests/architecture/test_guardrails_git_integration.py
python -m pytest -q
python -m ruff check scripts/architecture tests/architecture
python -m compileall -q scripts/architecture tests/architecture
```

Expected: all PASS.

- [ ] **Step 5: Commit the workflow**

```bash
git add .github/workflows/architecture-guard.yml \
  tests/architecture/test_guardrails_policy.py
git commit -m "ci: add base-trusted architecture guard"
```

---

### Task 7: Prove the governance PR contains no feature implementation and run final verification

**Files:**
- No new files expected.
- Verify all files created/modified by Tasks 1–6.

**Interfaces:**
- Consumes: completed guard implementation.
- Produces: merge-ready bootstrap governance PR candidate, still without Client API implementation.

- [ ] **Step 1: Verify the governance branch changed-path allowlist**

Run from the governance branch against its trusted base:

```bash
git diff --name-only origin/main...HEAD | sort
```

Expected paths only:

```text
.github/architecture/README.md
.github/architecture/frontiers/client-api-bootstrap-v1.json
.github/workflows/architecture-guard.yml
AGENTS.md
attention_router/core/client/AGENTS.md
contracts/client/v1/AGENTS.md
scripts/architecture/check_guardrails.py
tests/architecture/test_guardrails_git_integration.py
tests/architecture/test_guardrails_policy.py
tests/client/AGENTS.md
```

No Client API feature artifact from the reserved list may exist as a branch change.

- [ ] **Step 2: Run the checker tests and full existing offline suite**

```bash
python -m pytest -q tests/architecture/test_guardrails_policy.py \
  tests/architecture/test_guardrails_git_integration.py
python -m pytest -q
python -m ruff check .
python -m compileall -q attention_router tests scripts
```

Expected: PASS.

- [ ] **Step 3: Run repository contract-generation checks already required by Public CI**

```bash
python scripts/generate_integration_sdks.py --check
python scripts/test_python_integration_sdk.py
```

Expected: PASS; the governance change must not disturb existing integration SDK generation/conformance.

- [ ] **Step 4: Inspect for accidental Client API/runtime implementation**

Run:

```bash
git diff --name-only origin/main...HEAD | grep -E \
  'client-api\.openapi\.json|conformance-cases\.json|core/client/(authority|__init__)\.py|tests/client/test_|scripts/client_contract/|docs/architecture/client-api/' \
  && exit 1 || true
```

Expected: command exits successfully with no matches.

- [ ] **Step 5: Create the governance PR without merge**

Title:

```text
chore: install repository architecture guardrails
```

Body must state:

```markdown
## Scope
Installs trusted repository guardrails only. No Client API feature implementation, runtime mutation, database change, provider change, WhatsApp change, or deployment.

## Trusted policy
- governance prefix: `chore/architecture-governance/`
- first frontier: `client-api-bootstrap-v1`
- approved feature branch: `feat/client-api-bootstrap-v1`
- unrelated legacy-only PRs: `ARCH_NOT_APPLICABLE`

## Bootstrap note
This is the one-time installation PR. The base branch does not yet contain the Architecture Guard, so this PR is validated by the new guardrail tests plus the repository's existing required Public CI/CodeQL/secret-scan gates and human review. After merge, later governed PRs use the base-trusted guard.
```

Do not merge automatically.

- [ ] **Step 6: Verify existing required GitHub checks on the governance PR**

Required existing checks are currently:

```text
secret-scan
postgres-integration
docker-build
transport-tests
python-tests
analyze (javascript-typescript)
analyze (python)
```

Expected: all required checks PASS before asking for human merge approval.

- [ ] **Step 7: Commit any test-only fixes discovered by final verification**

If verification required a correction, make the smallest governance-only fix, rerun Steps 1–6, and commit with a focused message. Do not add feature implementation to make a test pass.

---

### Task 8: Merge the bootstrap governance PR and make `architecture-guard` a required main-branch check

**Files:**
- No repository file changes in this task unless review found a governance-only defect.
- GitHub repository ruleset change: `main-public-ci-protection`.

**Interfaces:**
- Consumes: reviewed green governance PR from Task 7.
- Produces: trusted guardrails on `main` and a blocking `architecture-guard` status requirement for future PRs.

- [ ] **Step 1: Merge only after explicit human approval and green existing required checks**

Do not bypass the active ruleset. Merge the governance PR using an allowed normal merge method only after review.

- [ ] **Step 2: Verify `main` contains the trusted guard artifacts**

Confirm on the merged `main`:

```text
.github/architecture/README.md
.github/architecture/frontiers/client-api-bootstrap-v1.json
.github/workflows/architecture-guard.yml
scripts/architecture/check_guardrails.py
```

Also verify the merged `main` commit's Public CI/CodeQL results before changing required-check configuration.

- [ ] **Step 3: Add `architecture-guard` to the active GitHub ruleset required status checks**

In GitHub repository settings:

```text
Settings
→ Rules
→ Rulesets
→ main-public-ci-protection
→ Required status checks
→ Add check: architecture-guard
→ Save changes
```

Preserve every currently required check. Do not remove or weaken:

```text
secret-scan
postgres-integration
docker-build
transport-tests
python-tests
analyze (javascript-typescript)
analyze (python)
```

- [ ] **Step 4: Verify the ruleset after the change**

Read the ruleset and confirm:

- enforcement remains `active`;
- target remains `refs/heads/main`;
- strict required status checks remain enabled;
- the seven pre-existing required checks remain present;
- `architecture-guard` is additionally required;
- no bypass actor was added.

- [ ] **Step 5: Verify legacy compatibility with a non-governed PR when the next suitable PR exists**

For an unrelated PR that changes only legacy/unmanaged paths, Architecture Guard must report:

```text
ARCHITECTURE_GUARD_STATUS=PASS
ARCHITECTURE_GUARD_RESULT=ARCH_NOT_APPLICABLE
```

Do not create a fake production-affecting change merely for this check; the unit/Git integration tests already prove the case.

---

### Task 9: Revise the Client API plan to conform to the now-trusted frontier

**Files:**
- Modify in a documentation-only planning change after guardrails are on `main`: `docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md`

**Interfaces:**
- Consumes: actual merged first frontier manifest.
- Produces: the plan that Codex will execute on exact branch `feat/client-api-bootstrap-v1`.

- [ ] **Step 1: Replace pre-guardrail paths with frontier paths**

Make these exact substitutions throughout the Client API plan:

```text
attention_router/core/client_authority.py
  -> attention_router/core/client/authority.py

tests/test_client_contract.py
  -> tests/client/test_contract.py

tests/test_client_contract_conformance.py
  -> tests/client/test_conformance.py

tests/test_client_authority.py
  -> tests/client/test_authority.py

scripts/check_client_contract.py
  -> scripts/client_contract/check.py

docs/client-api-bootstrap-v1.md
  -> docs/architecture/client-api/bootstrap-v1.md
```

- [ ] **Step 2: Add the frontier execution header to the Client API plan**

Immediately after the plan header/global context, add:

```markdown
**Frontier:** `client-api-bootstrap-v1`  
**Required implementation branch:** `feat/client-api-bootstrap-v1`

The feature executor must not modify `.github/architecture/**`, `scripts/architecture/**`, `.github/workflows/architecture-guard.yml`, root/scoped `AGENTS.md`, or any path outside the trusted frontier manifest. If execution requires another path or dependency, stop and report `FRONTIER_EXPANSION_REQUIRED` instead of widening scope.
```

- [ ] **Step 3: Remove any instruction that asks the Client API feature executor to edit CI/governance files**

The Client API feature plan must not ask Codex to modify:

```text
.github/architecture/**
scripts/architecture/**
.github/workflows/**
AGENTS.md
**/AGENTS.md
```

If the old plan asks to modify `.github/workflows/ci.yml`, remove that step. The standalone client contract checker is a feature artifact, but the architecture gate itself is already installed and trusted.

- [ ] **Step 4: Verify exact path equality with the trusted manifest**

Compare every feature file mentioned in the revised plan against the manifest `allowed_paths`. Every create/modify production/test/doc/script path must be in the manifest, with no extra feature path.

- [ ] **Step 5: Scan the revised Client API plan for placeholders and stale pre-guardrail paths**

Search for:

```text
TODO
TBD
client_authority.py
tests/test_client_contract.py
tests/test_client_contract_conformance.py
tests/test_client_authority.py
scripts/check_client_contract.py
docs/client-api-bootstrap-v1.md
.github/workflows/ci.yml
```

Expected: no stale implementation-path references or placeholders remain, except prose explicitly documenting the old-to-new migration if needed.

- [ ] **Step 6: Commit the documentation-only Client API plan revision**

```bash
git add docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md
git commit -m "docs: align client API plan with architecture frontier"
```

This plan revision is not Client API implementation and does not create the feature branch yet.

---

## Final Verification Before Codex Feature Handoff

After Tasks 1–9, verify all of the following before creating `feat/client-api-bootstrap-v1`:

```text
[ ] Guardrail design/spec is on trusted main.
[ ] Applicability amendment is on trusted main.
[ ] Architecture Guard checker/workflow/manifest are on trusted main.
[ ] architecture-guard is a required main ruleset status check.
[ ] Existing seven required checks remain required.
[ ] No bypass actor was added.
[ ] Scoped AGENTS.md files exist on trusted main.
[ ] Client API feature files do not yet exist from the governance work.
[ ] Client API implementation plan paths exactly match manifest allowed_paths.
[ ] Client API plan declares exact branch feat/client-api-bootstrap-v1.
[ ] No plan task tells the feature agent to mutate governance.
[ ] Full repository CI remains green on merged governance main.
```

Only then create `feat/client-api-bootstrap-v1` from the updated trusted `main` and hand it to Codex using the plan's required sub-skill/worktree workflow.

## Self-Review Checklist for This Plan

Before execution, confirm:

- **Spec coverage:** path allowlist, trusted-base judge, applicability, governance mode, frontier mode, import restrictions, contract boundary, required artifacts, symlink rejection, stable errors, bootstrap exception, ruleset activation, and Client API plan revision each have an explicit task.
- **Placeholder scan:** no `TODO`, `TBD`, "implement later", or unspecified error handling remains.
- **Type consistency:** `FrontierPolicy`, `GuardViolation`, `GuardMode`, `load_frontier_policies`, `resolve_mode`, `validate_changed_paths`, `validate_candidate_content`, `validate_required_artifacts`, and `run_guard` names are consistent across tasks.
- **Security:** candidate code is never checked out for execution/import; base workflow has `contents: read`, no secrets, and no write permission.
- **Scope:** governance PR cannot contain Client API feature artifacts; feature PR cannot contain governance artifacts.
