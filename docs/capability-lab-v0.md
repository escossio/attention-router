# Andy Capability Lab V0

Status: experimental engineering surface. The lab introduces no production authority and no second capability, grant, scenario, or human-authorization runtime.

## Purpose

The Andy Capability Lab is a development and validation cockpit for new Andy features. It is **not** the future customer settings experience.

The lab may expose technical detail when that helps engineering: capability identity, authority result, scenario state, evidence references, expiry, provenance and expected-versus-observed differences. A future end-user experience remains free to reduce the same decision to a compact interaction such as **allow once / always allow for this person / deny**.

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

Canonical components also already include:

- `ScenarioManifest` and immutable `ScenarioVersionRow`;
- `ScenarioRunRow` and `ScenarioStepRunRow`;
- L0/L1/L2/L3 scenario levels and effect classes;
- `SyntheticDriverV1`;
- `AcceptanceDefinition` / `AcceptanceResult`;
- `EvidenceReferenceRow` and typed evidence references;
- fail-closed budgets, leases, reconciliation and no-blind-retry rules.

The Capability Lab is a consumer and visualization layer over that infrastructure. It is not another scenario executor.

### Existing human authority

The runtime already contains `HumanExecutionAuthorizationRow` and durable human-approval evidence. Any future approval feature must reconcile with that boundary rather than create a second human-authorization system.

## What is new in the lab

`attention_router.core.capability_lab` contains **feature-expectation contracts only**.

A `CapabilityLabScenario` records:

- a stable lab scenario id;
- canonical capability key;
- synthetic requester and request context;
- expected `AuthorityResult` from the existing authority vocabulary;
- optional simulated human decision;
- an `ExpectedGrantMode` used only as an acceptance expectation;
- an optional `engine_scenario_key` binding;
- safety flags that permanently prohibit real personal data and production effects.

`ExpectedGrantMode` is deliberately not a replacement for `CapabilityGrantRow`. It describes what a feature experiment expects to prove: NONE, ONE_TIME, TIME_BOUND or PERSISTENT.

`CapabilityLabObservation` and `compare_scenario()` provide inert expected-versus-observed comparison. Results are:

- `PASS` — sufficient observed evidence matches the expectation;
- `FAIL` — resolution/grant mismatch, observation error or production effect;
- `INCOMPLETE` — evidence or an explicit engine binding is not yet sufficient.

Any observed production effect forces `FAIL`.

## Current synthetic feature hypotheses

The canonical fixture is:

`attention_router/web/static/capability-lab-scenarios.json`

It currently contains three hypotheses:

1. restricted identity disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> ONE_TIME grant expectation;
2. relationship-status disclosure -> `DENY` -> no grant;
3. current-location disclosure -> `REQUIRES_APPROVAL` -> simulated approval -> TIME_BOUND grant expectation.

All three currently have `engine_scenario_key = null`.

That is intentional. They are **UNBOUND feature hypotheses**, not claims that matching Scenario Engine manifests already exist. Until a feature hypothesis is deliberately bound to a real engine scenario and sufficient evidence is observed, the correct evaluation is `INCOMPLETE`, never an inferred `PASS`.

No real CPF, relationship value, coordinates, phone number or production contact identity is required to validate these semantics.

## A useful gap already exposed

The existing authority evaluator currently denies a capability when no active grant is available before it reaches the branch that can return `REQUIRES_APPROVAL`.

That may be correct for existing runtime use cases, but it is not automatically equivalent to the proposed disclosure feature:

`request without grant -> ask owner -> explicit approval -> optional derived grant`

The Capability Lab must expose that difference rather than hide it. The first business-feature experiment may legitimately start as `expected != observed`. A failing acceptance scenario is useful evidence that a runtime capability needs deliberate evolution.

No production authority behavior should be changed merely to make a lab card turn green.

## Read-only Scenario Engine bridge

`attention_router.platform.capability_lab_read_model.read_scenario_engine_snapshot()` projects existing Scenario Engine state without mutation.

It reads only:

- scenario definitions and immutable versions;
- recent scenario runs;
- step-status summaries;
- minimal evidence references.

It deliberately does not return evidence metadata payloads, request payloads, conversation content or other personal/operational details.

The response declares:

- `read_only = true`;
- `authority = OBSERVATION_ONLY`.

The current admin route is:

`GET /api/v1/admin/platform/operations/capability-lab/scenario-engine`

It is mounted inside the existing protected platform operations router to avoid introducing another application-router integration during V0. There is no POST, PATCH or DELETE counterpart.

## Web lab

The engineering surface currently lives at:

`/static/control-plane.html`

The legacy path is retained during V0; the visible identity is **Andy Capability Lab**.

The static feature hypotheses load without a runtime connection. Runtime evidence is optional and protected by the existing admin boundary.

The browser credential remains in page memory only and is not written to localStorage or sessionStorage.

## Invariants

1. Model output is never human approval.
2. Lab authentication is not execution authorization.
3. A lab click is not production authority.
4. The lab does not own a second capability registry.
5. The lab does not own a second grant store.
6. The lab does not own a second Scenario Engine.
7. The lab does not own a second human-approval system.
8. Public fixtures remain synthetic-only and contain no real personal values.
9. Unbound or insufficiently evidenced scenarios resolve to `INCOMPLETE`, never guessed success.
10. Ambiguous external effects remain fail-closed.
11. Engineering complexity visible in the lab must not dictate customer UX.

## Next safe boundary

1. Render the read-only Scenario Engine snapshot in the Capability Lab.
2. Clearly show the three current feature hypotheses as **UNBOUND / INCOMPLETE**.
3. Add an explicit binding mechanism only when we intentionally create or select an existing Scenario Engine manifest that proves a feature hypothesis.
4. Use the first binding to compare expected authority behavior against observed runtime evidence.
5. Only after evidence exposes the exact semantic gap should runtime capability/approval behavior be changed.

Persistence, production approval actions and external effects are outside this boundary.
