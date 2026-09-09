# Andy Control Plane V0

Status: experimental contract and UI bootstrap. No production authority is introduced by this document or by the V0 panel.

## Goal

Create a product-facing control plane for Andy without moving execution authority into the UI and without hard-coding each disclosure or action into conversational code.

The Control Plane is a configuration and observation surface. Attention Router remains the runtime that evaluates policy, preserves authority boundaries, creates execution intent, and records delivery evidence.

The first architectural unit is a governed capability. A capability is a stable identifier for something Andy may disclose or do, independent of the natural-language phrasing that caused the request.

Examples are synthetic identifiers only:

- `personal.identity.cpf`
- `personal.relationship.status`
- `location.current`
- `calendar.availability`
- `message.send`

The repository must not contain the represented owner's real values for those capabilities.

## Product boundary

The intended product split is:

1. **Attention Router Runtime** — conversation, policy, approval boundary, execution intent, outbox and delivery evidence.
2. **Andy Control Plane** — configuration, approval inbox, governed capabilities, grants, audit and tenant administration.
3. **Andy clients** — web today; future Android or other clients consume the same backend contracts.

The web panel is therefore not the product core. It is the first client of a transport-neutral contract.

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

## UI bootstrap

The first panel lives at:

`/static/control-plane.html`

It intentionally uses only current, already protected APIs:

- `/api/v1/admin/platform/operations/snapshot`
- `/api/v1/admin/policies`
- `/api/v1/admin/platform/matrix`
- `/api/v1/private/response-reviews?review_status=PENDING`
- `/api/v1/private/execution-intents?intent_status=PENDING`

The admin credential is held only in JavaScript memory for the current page. The V0 does not write it to localStorage or sessionStorage.

The panel distinguishes current runtime concepts from future Control Plane concepts. For example, platform device capabilities are not presented as the future governed disclosure/action catalog, and Response Review is not presented as the final AuthorizationRequest implementation.

## Explicit non-goals for V0

V0 does not:

- persist governed capabilities;
- create, update or delete approval rules;
- create permission grants;
- approve a HumanExecutionAuthorization;
- bypass the trusted human authority boundary;
- send messages or produce external effects;
- copy personal data into the public repository;
- change runtime policy or database schema.

This is deliberate. The contract and the client shell must stabilize before a mutation API is introduced.

## Security invariants

The following must remain true as the Control Plane grows:

1. Model output is never human approval.
2. UI authentication is not equivalent to execution authorization unless the server explicitly verifies the required human authority.
3. A grant is traceable to an explicit authorization.
4. Tenant and represented-subject scope are mandatory.
5. Capability values and personal data stay out of public source fixtures.
6. Ambiguous external effects fail closed.
7. Android, web and future clients share server contracts rather than reimplementing policy locally.

## Next implementation slices

### Slice 1 — registry persistence

Add tenant-scoped tables for governed capability definitions and immutable/versioned approval rules.

### Slice 2 — read API

Expose the capability catalog, rule evaluation explanation and active grants through admin-protected endpoints.

### Slice 3 — request ingestion

Translate an agent proposal such as "disclose CPF" into a stable capability request. Natural language never becomes the authorization key by itself.

### Slice 4 — trusted decision path

Bind Control Plane decisions to an authenticated human authority channel and create immutable AuthorizationRequest decisions.

### Slice 5 — grants

Create, consume, expire and revoke grants. Approval may produce NONE, ONE_TIME, TIME_BOUND or PERSISTENT authority according to the capability and matched rule.

### Slice 6 — product clients

Move the same contract behind a first-class authenticated Control Plane API. The current web surface becomes one client; Android can consume the same API without moving business rules into the app.

## Example behavior

A requester asks Andy for the owner's CPF.

1. Agent interpretation proposes `personal.identity.cpf`.
2. Control Plane policy resolves the request to REQUIRE_APPROVAL.
3. The owner receives an AuthorizationRequest.
4. Explicit approval permits the current disclosure.
5. If the matched rule says so, approval also creates a grant for that requester.
6. A later request uses the grant only while scope and validity still match.
7. Revocation or expiry returns the request to the normal policy path.

The actual CPF value is not part of the capability definition or approval rule.
