# Repository Guardrails — Applicability Amendment

Status: **design clarification, not implemented**  
Date: 2026-09-11  
Repository: `escossio/attention-router`  
Amends: `docs/superpowers/specs/2026-09-11-repository-guardrails-design.md`

This amendment resolves one applicability ambiguity discovered while preparing the implementation plan. It does not broaden the approved guardrail scope or change the first frontier. All sections of the base guardrail design remain authoritative except where this amendment makes applicability explicit.

## 1. Why this clarification is required

The base design has two simultaneous requirements:

- legacy repository areas remain outside the initial guardrail blast radius; and
- unknown or untrusted frontier selection fails closed.

A workflow that failed every pull request whose branch was not assigned to a frontier would accidentally block unrelated legacy work and Dependabot updates. That would contradict the approved legacy-compatibility rule.

Therefore frontier resolution is performed **after an applicability check**.

## 2. Applicability algorithm

For every pull request targeting the protected default branch, the base-trusted Architecture Guard evaluates in this order:

1. If the head branch starts with the trusted governance prefix `chore/architecture-governance/`, run **governance mode**.
2. Otherwise, if the exact head branch matches exactly one trusted frontier manifest, run that **feature frontier**.
3. Otherwise, inspect changed paths using trusted policy metadata.
4. If the unassigned branch touches any trusted governance path or any path owned by a governed frontier/root, fail closed:
   - governance path mutation -> `ARCH_GOVERNANCE_MUTATION`;
   - governed implementation area without trusted branch assignment -> `ARCH_FRONTIER_UNKNOWN`.
5. If the unassigned branch touches only legacy/unmanaged paths, return success with result `ARCH_NOT_APPLICABLE`.

`ARCH_NOT_APPLICABLE` is a successful guard result, not an authorization mechanism. It means only that the pull request is outside the currently governed repository territory and remains subject to the repository's other CI/review rules.

## 3. Governed roots for the first installation

The first guard installation treats these as governed/trusted territory:

- `contracts/client/**`;
- `attention_router/core/client/**`;
- `tests/client/**`;
- `scripts/client_contract/**`;
- `docs/architecture/client-api/**`;
- `.github/architecture/**`;
- `scripts/architecture/**`;
- `.github/workflows/architecture-guard.yml`;
- root/scoped governance `AGENTS.md` paths created by the guardrail installation.

The first frontier continues to own only its exact allowlisted feature files inside those roots.

## 4. Fail-closed semantics after applicability

Once a pull request is applicable to guardrails, fail-closed behavior remains unchanged:

- zero trusted frontier assignments for a governed feature change -> `ARCH_FRONTIER_UNKNOWN`;
- more than one matching trusted assignment -> `ARCH_POLICY_INVALID`;
- candidate-controlled frontier selection -> rejected;
- feature mutation of trusted governance -> `ARCH_GOVERNANCE_MUTATION`;
- malformed trusted policy -> `ARCH_POLICY_INVALID`.

## 5. Test requirement added by this amendment

The guardrail test suite must include all of these cases:

- unrelated legacy-only PR on an unassigned branch -> PASS with `ARCH_NOT_APPLICABLE`;
- Dependabot-style legacy dependency PR that does not touch governed roots -> PASS with `ARCH_NOT_APPLICABLE`;
- unassigned branch modifying `contracts/client/**` -> FAIL `ARCH_FRONTIER_UNKNOWN`;
- unassigned branch modifying trusted governance -> FAIL `ARCH_GOVERNANCE_MUTATION`;
- exact `feat/client-api-bootstrap-v1` branch -> resolves `client-api-bootstrap-v1` and receives full frontier validation.

## 6. Bootstrap installation exception

The very first governance-only PR installs the Architecture Guard itself, so the base branch cannot yet use that not-yet-existing guard to judge the same PR.

That one bootstrap PR is therefore validated by:

- the guardrail unit/integration tests executed in the candidate through the existing Public CI test environment where safe;
- existing required Public CI and CodeQL checks;
- secret scanning;
- human review against the approved guardrail design and implementation plan;
- explicit confirmation that the PR contains governance-only paths and no Client API feature implementation.

After the bootstrap governance PR is merged, all later governance and governed-feature pull requests are evaluated by the base-trusted Architecture Guard described in the design.

This is a one-time bootstrap condition, not a permanent bypass.
