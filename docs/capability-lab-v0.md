# Andy Capability Lab V0

Status: experimental engineering surface. The lab introduces no production authority and no second capability, grant, scenario, or human-authorization runtime.

## Purpose

The Andy Capability Lab is a development and validation cockpit for new Andy features. It is **not** the future customer settings experience.

The lab may expose technical detail when that helps engineering: capability identity, registry state, authority result, human-decision state, grant state, scenario state, evidence references, expiry, provenance and expected-versus-observed differences. A future end-user experience remains free to reduce the same decision to a compact interaction such as **allow once / always allow for this person / deny**.

## Canonical runtime already exists

Repository inspection during the V0 bootstrap established that Attention Router already contains the runtime contracts the lab must exercise rather than duplicate.

### Capability runtime

Canonical components include:

- `attention_router.core.capabilities.CapabilityRequest` and `CapabilityResolution`;
- `attention_router.core.authority.AuthorityResult` and `evaluate_effective_authority()`;
- versioned capability manifests in `config/platform/capabilities.v1.json`;
- `CapabilityDefinitionRow` and `CapabilityVersionRow`;
- `CapabilityGrantRow`;
- provider definitions, instances and bindings;
- `attention_router.application.platform.registry.resolve_capability_request()`.

Therefore the Capability Lab does **not** define a second `CapabilityDefinition`, persisted permission-grant model, provider registry or authority engine.

### Scenario and evidence runtime

Canonical components already include:

- `ScenarioManifest` and immutable `ScenarioVersionRow`;
- `ScenarioRunRow` and `ScenarioStepRunRow`;
- L0/L1/L2/L3 scenario levels and effect classes;
- `SyntheticDriverV1`;
- `AcceptanceDefinition` / `AcceptanceResult`;
- `EvidenceReferenceRow` and typed evidence references;
- fail-closed budgets, leases, reconciliation and no-blind-retry rules.

The Capability Lab is a consumer and visualization layer over that infrastructure. It is not another scenario executor.

### Existing human authority and grants

The runtime already contains `HumanExecutionAuthorizationRow` and durable human-approval evidence. `prepare()`, `request_approval()` and `decide()` own the canonical human execution authorization lifecycle.

The runtime also already contains `create_capability_grant()` and `revoke_capability_grant()` over `CapabilityGrantRow`.

Any future approval-to-grant feature must bridge these existing boundaries deliberately. The Lab must not create a second human-authorization system or second grant store.

## What is new in the Lab

`attention_router.core.capability_lab` contains **feature-expectation contracts only**.

A `CapabilityLabScenario` records:

- a stable lab scenario id;
- canonical capability key, validated with the runtime's `CAPABILITY_NAME_RE`;
- synthetic requester and request context;
- expected `AuthorityResult` from the existing authority vocabulary;
- optional simulated human decision;
- an `ExpectedGrantMode` used only as an acceptance expectation;
- an optional `engine_scenario_key` binding;
- safety flags that permanently prohibit real personal data and production effects.

`ExpectedGrantMode` is deliberately not a replacement for `CapabilityGrantRow`. It describes what a feature experiment expects to prove: NONE, ONE_TIME, TIME_BOUND or PERSISTENT.

## Staged comparison: T0, T1 and T2

Approval flows are not one atomic observation. The Lab compares them as three explicit stages:

- **T0 — authority resolution**: did the canonical capability/authority runtime return the expected `AuthorityResult` and reason?
- **T1 — human decision**: when the hypothesis includes a decision, was the expected APPROVE or DENY decision observed through the human-authority boundary?
- **T2 — derived grant**: when approval is expected to create reusable authority, was the expected grant behavior observed?

Each observed stage has its **own evidence references**:

- `resolution_evidence_refs`;
- `human_decision_evidence_refs`;
- `grant_evidence_refs`.

A semantically correct observation without its stage evidence is still `INCOMPLETE`.

Examples:

- expected T0 is correct, T1 not observed -> `INCOMPLETE`;
- T0 and T1 are correct, T2 not observed -> `INCOMPLETE`;
- T0/T1/T2 values are correct but T2 evidence is missing -> `INCOMPLETE`;
- any expected/observed mismatch -> `FAIL`;
- any production effect observed in a Lab scenario -> `FAIL`;
- all required stages plus evidence match -> `PASS`.

A direct DENY or ALLOW hypothesis may complete at T0, but T0 still requires evidence before it can be certified PASS.

## Current synthetic feature hypotheses

The canonical fixture is:

`attention_router/web/static/capability-lab-scenarios.json`

It currently contains three hypotheses:

1. restricted identity disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> ONE_TIME grant expectation;
2. relationship-status disclosure -> `DENY` -> no grant;
3. current-location disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> TIME_BOUND grant expectation.

All three currently have `engine_scenario_key = null` and are deliberately **UNBOUND**.

Repository inspection also establishes their current registry status:

- `personal.identity.cpf` -> **UNREGISTERED** in the canonical capability manifest;
- `personal.relationship.status` -> **UNREGISTERED**;
- `location.current` -> **REGISTERED**, but currently `PROVIDER_MISSING` and backed by `LocationProvider`.

These are different feature-development states. The Lab should expose that difference instead of pretending all three are ready for the same experiment.

No real CPF, relationship value, coordinates, phone number or production contact identity is required to validate these semantics.

## Authority gaps must be observed, not patched to green

The existing authority evaluator currently checks active grant availability before it can return `REQUIRES_APPROVAL`. For an otherwise operational capability with no active grant, the current result is:

`DENY / CAPABILITY_GRANT_MISSING`

That behavior may be correct for current runtime use cases, but it is not automatically equivalent to the proposed feature flow:

`request without grant -> ask owner -> explicit approval -> optional derived grant`

A Lab test explicitly preserves this current behavior as a known semantic gap. Runtime authority must not be changed merely to make a Lab card green.

There is an additional prerequisite for the CPF hypothesis: because `personal.identity.cpf` is not yet a registered canonical capability, a real `resolve_capability_request()` currently reaches `UNKNOWN_CAPABILITY / UNAVAILABLE` before the no-grant question is even relevant.

## Read-only Scenario Engine bridge

`attention_router.platform.capability_lab_read_model.read_scenario_engine_snapshot()` projects existing Scenario Engine state without mutation.

It reads only:

- scenario definitions and immutable versions;
- recent scenario runs;
- step-status summaries;
- minimal evidence references.

The projection deliberately excludes evidence metadata payloads, conversation content, `correlation_id`, `terminal_reason` and other detail not needed by V0. Tenant isolation is tested explicitly.

The response declares:

- `read_only = true`;
- `authority = OBSERVATION_ONLY`.

The current admin route is:

`GET /api/v1/admin/platform/operations/capability-lab/scenario-engine`

There is no POST, PATCH or DELETE counterpart.

## Important L0 limitation

The existing `execute_l0_manifest()` is a strong **safety-boundary dry-run executor**, but it is not yet a generic business-semantics evaluator.

Its current L0 assertion path records the dry-run contract as satisfied and evaluates the assertion predicate with `observed=True`. Therefore a newly written manifest that merely says "CPF should require approval" could become green without actually invoking `resolve_capability_request()` or `evaluate_effective_authority()`.

For that reason:

- do **not** create or bind a new capability hypothesis to a generic L0 manifest merely to obtain a ScenarioRun;
- do **not** treat an ordinary L0 PASS as proof of capability/approval semantics;
- the first capability binding requires a semantic probe/evaluator that delegates to the canonical runtime and emits real stage evidence.

`SCN-PE-028` (`ai self approval attempt`) is a useful neighboring safety scenario proving that AI cannot self-approve, but it is **not** equivalent to the feature flow "missing grant -> human approval -> derived grant" and must not be reused through fuzzy matching.

## Web Lab

The engineering surface currently lives at:

`/static/control-plane.html`

The legacy path is retained during V0; the visible identity is **Andy Capability Lab**.

The static feature hypotheses load without a runtime connection. Runtime observation is optional and protected by the existing admin boundary.

The browser credential remains in page memory only and is not written to localStorage or sessionStorage. The frontend performs no POST, PATCH or DELETE calls.

## Invariants

1. Model output is never human approval.
2. Lab authentication is not execution authorization.
3. A Lab click is not production authority.
4. The Lab does not own a second capability registry.
5. The Lab does not own a second grant store.
6. The Lab does not own a second Scenario Engine.
7. The Lab does not own a second human-approval system.
8. Public fixtures remain synthetic-only and contain no real personal values.
9. Unbound or insufficiently evidenced hypotheses resolve to `INCOMPLETE`, never guessed success.
10. A semantically correct stage without stage evidence remains `INCOMPLETE`.
11. A generic L0 dry-run PASS is not automatically business-semantic evidence.
12. Ambiguous external effects remain fail-closed.
13. Engineering complexity visible in the Lab must not dictate customer UX.

## Next safe boundary

1. Expose canonical registry status beside each feature hypothesis: UNREGISTERED, registered state or provider availability.
2. Add a **read-only semantic capability probe** that calls the canonical `resolve_capability_request()` for a synthetic requester and maps the result to T0 observation only.
3. Keep that ephemeral probe `INCOMPLETE` until a durable T0 evidence reference exists.
4. Use an already registered, operational, no-external-effect capability as a control case before registering new personal-disclosure capabilities.
5. Only after T0 is trustworthy should a dedicated Scenario Engine semantic evaluator/binding be designed.
6. T1 must use canonical HumanExecutionAuthorization evidence; T2 must use canonical CapabilityGrant evidence.
7. Only after these stages expose the exact gap should runtime authority behavior be changed.

Persistence of new product capabilities, production approval actions, external effects and customer UX are outside this V0 boundary.
