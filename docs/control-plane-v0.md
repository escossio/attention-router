# Andy Capability Lab V0 and Control Plane contracts

Status: experimental engineering surface and contract bootstrap. No production authority is introduced by this document or by the V0 lab.

## Goal

Create a development and validation surface for Andy capabilities without turning that surface into the future end-user product UI and without hard-coding each disclosure or action into conversational code.

The lab exists to prove features, inspect state transitions, exercise synthetic scenarios, expose decision reasons and validate authority boundaries. Attention Router remains the runtime that evaluates policy, preserves authority, creates execution intent and records delivery evidence.

The first architectural unit is a governed capability: a stable identifier for something Andy may disclose or do, independent of the natural-language phrasing that caused the request.

Synthetic examples:

- `personal.identity.cpf`
- `personal.relationship.status`
- `location.current`
- `calendar.availability`
- `message.send`

The repository must never contain the represented owner's real values for those capabilities.

## Boundary: runtime, contracts, lab and product UX

Four concepts must stay separate:

1. **Attention Router Runtime** — conversation, policy, approval boundary, execution intent, outbox and delivery evidence.
2. **Control Plane contracts** — transport-neutral representations for governed capabilities, approval rules, authorization requests and permission grants.
3. **Andy Capability Lab** — engineering cockpit used to observe, simulate and validate those concepts during development.
4. **End-user UX** — future WhatsApp interactions, notifications, Android surfaces or other simple interfaces designed for the person using Andy.

The Capability Lab is explicitly not a prototype of the final customer settings experience. Technical concepts such as rule priority, selectors, TTL, scope and provenance may be visible in the lab even when they should never appear in the end-user UX.

The commercial experience should be free to reduce a complex internal decision to something as simple as: allow once, always allow for this person, or deny.

## V0 contract

`attention_router.core.control_plane` introduces four contract families.

### CapabilityDefinition

Describes what is governed, not its real value.

Important fields:

- stable `key`;
- `kind`: DISCLOSURE, ACTION, DELEGATION or AUTOMATION;
- `sensitivity`;
- default disposition: ALLOW, DENY or REQUIRE_APPROVAL;
- supported grant modes;
- optional provider key;
- extensible metadata.

`GrantMode.NONE` is exclusive. A capability may instead support one or more grantable modes.

### ApprovalRule

Describes how a request for one capability should be resolved.

The selectors are data, not Python branches:

- requester selector;
- context selector;
- resolution;
- optional grant created after explicit approval;
- optional TTL for a time-bounded grant;
- priority and enabled state.

A rule that does not require approval cannot silently create a grant.

The fact that these technical fields exist does not mean the end user will configure them directly. They are engine concepts first and lab-observable concepts second.

### AuthorizationRequest

Represents one request that reached the human authority boundary.

The initial contract preserves:

- capability;
- represented subject;
- requester;
- state;
- request/expiry window;
- matched rule;
- optional resulting grant;
- correlation and provenance.

Only an APPROVED request may reference a resulting grant.

### PermissionGrant

Represents reusable authority derived from an explicit approval.

The initial contract preserves:

- capability;
- represented subject;
- grantee;
- grant mode;
- validity;
- originating authorization;
- constraints and provenance.

A persisted grant cannot use `NONE`. A TIME_BOUND grant requires an expiry.

## Capability Lab V0

The current engineering surface lives at:

`/static/control-plane.html`

The legacy path name is acceptable for V0 because the page consumes Control Plane concepts, but the user-facing identity of the surface is **Andy Capability Lab**.

It intentionally reads only current protected APIs:

- `/api/v1/admin/platform/operations/snapshot`
- `/api/v1/admin/policies`
- `/api/v1/admin/platform/matrix`
- `/api/v1/private/response-reviews?review_status=PENDING`
- `/api/v1/private/execution-intents?intent_status=PENDING`

The admin credential is held only in JavaScript memory for the current page. V0 does not write it to localStorage or sessionStorage.

The lab distinguishes current runtime concepts from future governed-capability concepts. Platform device capabilities are not presented as the future disclosure/action catalog, and Response Review is not presented as the final AuthorizationRequest implementation.

## What the lab should become

The lab should help answer questions such as:

- Did a synthetic request map to the expected capability?
- Which rule matched and why?
- Did the request correctly require approval, deny or allow?
- Did an approval produce the expected grant mode?
- Did a one-time grant get consumed exactly once?
- Did a time-bound grant expire at the right boundary?
- Did revocation restore the normal approval path?
- Did tenant and represented-subject scope remain isolated?
- Can the same feature be tested without a real WhatsApp conversation or production data?

A strong future lab can include scenario fixtures, replay, state-machine visualization, rule explanation, synthetic actor selection, time travel for expiry tests, audit correlation and comparison between expected and observed outcomes.

## Explicit non-goals for V0

V0 does not:

- define the final Android or WhatsApp UX;
- act as the customer's main settings panel;
- persist governed capabilities;
- create, update or delete approval rules;
- create permission grants;
- approve a HumanExecutionAuthorization;
- bypass the trusted human authority boundary;
- send messages or produce external effects;
- copy personal data into the public repository;
- change runtime policy or database schema.

This is deliberate. The contracts and validation approach must stabilize before mutation APIs are introduced.

## Security and product invariants

The following must remain true as the feature grows:

1. Model output is never human approval.
2. Lab authentication is not equivalent to execution authorization.
3. A grant is traceable to an explicit authorization.
4. Tenant and represented-subject scope are mandatory.
5. Capability values and personal data stay out of public source fixtures.
6. Ambiguous external effects fail closed.
7. Product clients consume server contracts rather than reimplement policy locally.
8. The lab may expose engineering complexity that the end-user UX intentionally hides.
9. No design choice in the lab should force the future customer experience to use forms, priorities or raw policy fields.

## Next implementation slices

### Slice 1 — explainability fixtures

Add synthetic capability scenarios with expected outcomes: ALLOW, DENY, REQUIRE_APPROVAL and resulting grant behavior. The lab should show expected versus observed state without production mutation.

### Slice 2 — relationship with existing human authorization

Reconcile product-level AuthorizationRequest with the existing `HumanExecutionAuthorizationRow` and durable human-approval evidence. Do not create a second competing authority model.

### Slice 3 — registry persistence

Only after that reconciliation, add tenant-scoped persistence for governed capability definitions and versioned rules where persistence is actually required.

### Slice 4 — read and explanation API

Expose capability catalog, rule evaluation explanation and active grants through protected endpoints suitable for the lab and future clients.

### Slice 5 — request ingestion

Translate an agent proposal such as "disclose CPF" into a stable capability request. Natural language never becomes the authorization key by itself.

### Slice 6 — trusted decision path and grants

Bind approval decisions to authenticated human authority and validate creation, consumption, expiry and revocation of ONE_TIME, TIME_BOUND and PERSISTENT grants.

### Slice 7 — end-user experience

Design the real interaction independently from the lab. A user-facing approval can be a compact card, notification or conversational prompt even though the lab exposes the full underlying state.

## Example validation scenario

A synthetic requester asks Andy for the owner's CPF.

1. Agent interpretation proposes `personal.identity.cpf`.
2. Rule evaluation resolves the request to REQUIRE_APPROVAL.
3. The lab displays the matched capability, rule and reason.
4. A controlled test decision simulates explicit approval without pretending to be production authority.
5. The scenario expects a specific grant behavior, such as ONE_TIME or TIME_BOUND.
6. A second synthetic request verifies whether the grant is honored correctly.
7. Expiry, consumption or revocation returns the scenario to the normal approval path.
8. The lab reports expected versus observed behavior and correlated evidence.

The actual CPF value is never required to validate this behavior.
