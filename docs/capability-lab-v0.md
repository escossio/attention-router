# Andy Capability Lab V0

Status: experimental engineering surface. The Lab introduces no production authority and no second capability, grant, scenario, or human-authorization runtime.

## Purpose

The Andy Capability Lab is a development and validation cockpit for new Andy features. It is **not** the future customer settings experience.

The Lab may expose technical detail when that helps engineering: capability identity, registry state, authority result, human-decision state, grant state, scenario state, evidence references, expiry, provenance and expected-versus-observed differences. A future end-user experience remains free to reduce the same decision to a compact interaction such as **allow once / always allow for this person / deny**.

## Canonical runtime already exists

Repository inspection during the V0 bootstrap established that Attention Router already contains the runtime contracts the Lab must exercise rather than duplicate.

### Capability runtime

Canonical components include:

- `attention_router.core.capabilities.CapabilityRequest` and `CapabilityResolution`;
- `attention_router.core.authority.AuthorityResult` and `evaluate_effective_authority()`;
- versioned capability manifests in `config/platform/capabilities.v1.json`;
- `CapabilityDefinitionRow` and `CapabilityVersionRow`;
- `CapabilityGrantRow`;
- provider definitions, instances and bindings;
- `attention_router.application.platform.registry.resolve_capability_request()`.

The Capability Lab therefore does **not** define a second capability definition, provider registry, grant store or authority engine.

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

## Feature hypotheses

`attention_router.core.capability_lab` contains **feature-expectation contracts only**.

A `CapabilityLabScenario` records:

- a stable Lab scenario id;
- a capability key validated with the runtime's canonical `CAPABILITY_NAME_RE`;
- a synthetic requester and request context;
- an expected `AuthorityResult`;
- an optional simulated human decision;
- an `ExpectedGrantMode` used only as an acceptance expectation;
- an optional explicit `engine_scenario_key` binding;
- safety flags that permanently prohibit real personal data and production effects.

`ExpectedGrantMode` is deliberately not a replacement for `CapabilityGrantRow`. It describes what a feature experiment expects to prove: NONE, ONE_TIME, TIME_BOUND or PERSISTENT.

The canonical fixture is:

`attention_router/web/static/capability-lab-scenarios.json`

It currently contains three hypotheses:

1. restricted identity disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> ONE_TIME grant expectation;
2. relationship-status disclosure -> `DENY` -> no grant;
3. current-location disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> TIME_BOUND grant expectation.

All three currently have `engine_scenario_key = null` and are deliberately **UNBOUND**.

No real CPF, relationship value, coordinates, phone number or production contact identity is required to validate these semantics.

## Staged comparison: T0, T1 and T2

Approval flows are not one atomic observation. The Lab compares three explicit stages:

- **T0 — authority resolution**: did the canonical capability/authority runtime return the expected `AuthorityResult` and reason?
- **T1 — human decision**: when the hypothesis includes a decision, was the expected APPROVE or DENY observed through the human-authority boundary?
- **T2 — derived grant**: when approval is expected to create reusable authority, was the expected grant behavior observed?

Each observed stage has its own evidence references:

- `resolution_evidence_refs`;
- `human_decision_evidence_refs`;
- `grant_evidence_refs`.

A semantically correct observation without its stage evidence is still `INCOMPLETE`.

Examples:

- T0 value matches, T0 evidence missing -> `INCOMPLETE`;
- T0 is evidenced, T1 not observed -> `INCOMPLETE`;
- T0/T1 match, T2 not observed or evidenced -> `INCOMPLETE`;
- any expected/observed mismatch -> `FAIL`;
- any production effect observed in a Lab scenario -> `FAIL`;
- all required stages plus durable evidence match -> `PASS`.

A direct DENY or ALLOW hypothesis may complete at T0, but T0 still requires evidence before it can be certified PASS.

## Current registry state of the three hypotheses

Repository inspection establishes three different development states:

- `personal.identity.cpf` -> **UNREGISTERED** in the canonical capability manifest;
- `personal.relationship.status` -> **UNREGISTERED**;
- `location.current` -> **REGISTERED**, currently `PROVIDER_MISSING`, provider interface `LocationProvider`.

The Lab renders this registry state separately from Scenario Engine binding and separately from T0 authority observation.

## Known no-grant authority gap

The existing authority evaluator checks active grant availability before it can return `REQUIRES_APPROVAL`. For an otherwise operational capability with no active grant, the current result is:

`DENY / CAPABILITY_GRANT_MISSING`

That behavior may be correct for current runtime use cases, but it is not automatically equivalent to the proposed feature flow:

`request without grant -> ask owner -> explicit approval -> optional derived grant`

A Lab test preserves this current behavior as a known semantic gap. The operational `callback.request` capability is used as a no-external-effect control and currently demonstrates:

`AVAILABLE_NOT_AUTHORIZED -> DENY / CAPABILITY_GRANT_MISSING`

Runtime authority must not be changed merely to make a Lab card green.

For CPF there is an earlier prerequisite: because `personal.identity.cpf` is not registered, canonical resolution currently stops at:

`UNKNOWN -> UNAVAILABLE / UNKNOWN_CAPABILITY`

## Read-only T0 semantic probe

`attention_router.platform.capability_lab_probe` delegates directly to the canonical `resolve_capability_request()`.

The probe is deliberately constrained:

- it accepts a repository-owned `scenario_id`, not a client-defined capability request;
- the capability key and synthetic requester come from the versioned Lab fixture;
- HTTP clients cannot supply `capability_key`, `requester_actor_key`, `policy_allows`, tenant, grant state or `owner_authorized`;
- `policy_allows=True` and `owner_authorized=False` are fixed server-side for this V0 observation;
- it performs no grant creation, approval action, ScenarioRun creation or external effect;
- it creates no durable evidence.

The protected route is:

`GET /api/v1/admin/platform/operations/capability-lab/probe/{scenario_id}`

Unknown ids fail closed with `CAPABILITY_LAB_SCENARIO_UNKNOWN`.

The response is marked:

`certification = EPHEMERAL_ONLY`

and:

`durable_evidence = false`

The server also runs the normal `compare_scenario()` function against the ephemeral T0 observation.

Important interpretation:

- an ephemeral **mismatch** may immediately surface as `FAIL` because fail-closed diagnosis should not hide a contradiction;
- an ephemeral **match** cannot become PASS, because T0 evidence is absent; it remains `INCOMPLETE / T0_RESOLUTION_EVIDENCE_MISSING`;
- the probe is a diagnostic microscope, not certification evidence.

Current expected probe results after registry synchronization are:

- CPF hypothesis -> `UNKNOWN / UNAVAILABLE / UNKNOWN_CAPABILITY`;
- relationship hypothesis -> `UNKNOWN / UNAVAILABLE / UNKNOWN_CAPABILITY`;
- location hypothesis -> `KNOWN_BUT_UNAVAILABLE / UNAVAILABLE / CAPABILITY_UNAVAILABLE`, `LocationProvider`;
- operational control `callback.request` -> `AVAILABLE_NOT_AUTHORIZED / DENY / CAPABILITY_GRANT_MISSING`.

Tests prove the probe leaves registry/provider/grant row counts unchanged and leaves the SQLAlchemy session with no new, dirty or deleted rows.

## Durable T0 semantic evidence

`record_capability_t0_evidence()` adds the first evidence-producing certification boundary without changing authority semantics.

The sequence is deliberately narrow:

`canonical resolve_capability_request() -> synthetic OperationalObservationRow -> EvidenceReferenceRow -> CapabilityLabObservation -> compare_scenario()`

The durable observation stores only minimized technical fields:

- T0 stage;
- Lab scenario id;
- canonical capability name;
- capability resolution status;
- authority result;
- approval-required and execution-allowed booleans;
- synthetic lineage and explicit no-production-effect marker;
- source, runtime and schema revisions.

It does **not** store request text, request context, requester identity, message payloads, provider credentials or personal values.

The `EvidenceReferenceRow` uses the existing `API_RESULT` evidence type and points to the persisted `OperationalObservationRow`; the reference does not duplicate the evidence payload.

The report is marked:

`certification = DURABLE_T0_ONLY`

This means T0 can now become a real `PASS` when expected and observed authority match and the evidence reference is present. It says nothing about T1 or T2.

The first control is deliberately not one of the new personal capabilities. It uses existing `callback.request`, which is operational and has no external effect. With an expectation matching current behavior (`DENY`), the control can certify T0 PASS with durable evidence. With an expectation of `REQUIRES_APPROVAL`, the same evidence remains FAIL because the canonical runtime currently returns `DENY / CAPABILITY_GRANT_MISSING`.

The evidence-producing function is **not exposed through the GET probe route or browser V0**. Transaction commit remains caller-owned. It creates no `CapabilityGrantRow`, no `HumanExecutionAuthorizationRow`, no `ScenarioRunRow`, no outbox item and no external call.

Both SQLite contract tests and a dedicated PostgreSQL integration test verify this boundary, including persistence after commit and unchanged authority-side-effect row counts.

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

The protected route is:

`GET /api/v1/admin/platform/operations/capability-lab/scenario-engine`

There is no POST, PATCH or DELETE counterpart.

## Important L0 limitation

The existing `execute_l0_manifest()` is a strong safety-boundary dry-run executor, but it is not yet a generic business-semantics evaluator.

Its current L0 assertion path records the dry-run contract as satisfied and evaluates the assertion predicate with `observed=True`. A newly written manifest that merely says "CPF should require approval" could therefore become green without actually invoking `resolve_capability_request()` or `evaluate_effective_authority()`.

For that reason:

- do **not** create or bind a new capability hypothesis to a generic L0 manifest merely to obtain a ScenarioRun;
- do **not** treat an ordinary L0 PASS as proof of capability/approval semantics;
- the first capability binding requires a semantic evaluator that delegates to the canonical runtime and emits real stage evidence.

`SCN-PE-028` (`ai self approval attempt`) proves that AI cannot self-approve, but it is not equivalent to the feature flow "missing grant -> human approval -> derived grant" and must not be reused through fuzzy matching.

## Web Lab

The engineering surface currently lives at:

`/static/control-plane.html`

The legacy path is retained during V0; the visible identity is **Andy Capability Lab**.

The three static hypotheses load without runtime access. After authenticated connection, the Lab displays:

- canonical registry state;
- read-only T0 probe result and reason;
- explicit `EPHEMERAL_ONLY / durable evidence NO` status;
- explicit Scenario Engine binding state;
- read-only persisted Scenario Engine evidence.

The browser sends only each repository scenario id to the probe endpoint. It never sends capability, requester, policy or owner-authority controls.

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
9. Unbound or insufficiently evidenced hypotheses never become guessed success.
10. A semantically correct stage without stage evidence remains `INCOMPLETE`.
11. A generic L0 dry-run PASS is not automatically business-semantic evidence.
12. An ephemeral T0 match is not certification evidence.
13. Durable T0 evidence may certify only T0; it grants no execution authority.
14. GET observation routes remain read-only and never create evidence as a hidden side effect.
15. Ambiguous external effects remain fail-closed.
16. Engineering complexity visible in the Lab must not dictate customer UX.

## Next safe boundary

1. Validate the durable T0 evaluator in the full Public CI and complete PostgreSQL suite.
2. After a green checkpoint, design the explicit Scenario Engine semantic binding that invokes this T0 evaluator rather than the generic L0 `observed=True` path.
3. Keep the three current personal-feature hypotheses UNBOUND until a real semantic manifest/evaluator proves their exact contract.
4. Register new personal-disclosure capabilities only through the canonical capability manifest/registry when their contracts are mature.
5. T1 must use canonical `HumanExecutionAuthorization` evidence.
6. T2 must use canonical `CapabilityGrant` evidence.
7. Only after T0/T1/T2 expose the exact semantic gap should runtime authority behavior change.

Production approval actions, external effects and customer UX are outside this V0 boundary.
