# Andy Capability Lab V0 and Control Plane contracts

Status: experimental engineering surface and contract bootstrap. No production authority is introduced by this document or by the V0 lab.

## Purpose

The Andy Capability Lab is a development and validation cockpit for new Andy features. It is **not** the future customer settings experience.

The lab exists to expose technical detail when that helps engineering: capability keys, matched rules, priority, scope, TTL, provenance, authorization state, grant state and correlated evidence. A future end-user experience is free to hide all of that behind a simple interaction such as **allow once / always allow for this person / deny**.

Attention Router remains the runtime that evaluates policy, preserves authority boundaries, creates execution intent, manages outbox work and records delivery evidence.

## Four boundaries that must stay separate

1. **Attention Router Runtime** — conversation, policy, approval boundary, execution intent, outbox and delivery evidence.
2. **Control Plane contracts** — transport-neutral representations for capabilities, rules, authorization requests and permission grants.
3. **Andy Capability Lab** — engineering surface used to observe, simulate and validate those concepts.
4. **End-user UX** — future WhatsApp, notification, Android or other simple customer-facing interactions.

No design choice in the lab should force the future user to configure raw policies, priorities, selectors or TTL fields.

## Governed capability contract

`attention_router.core.control_plane` defines the V0 contract families.

### CapabilityDefinition

A stable identifier for something Andy may disclose or do, independent of natural-language phrasing.

Synthetic examples:

- `personal.identity.cpf`
- `personal.relationship.status`
- `location.current`
- `calendar.availability`
- `message.send`

A capability definition contains metadata and governance properties, never the represented owner's real value.

### ApprovalRule

Describes how a capability request should resolve. Selectors and resolution are data rather than Python branches. A rule may resolve to ALLOW, DENY or REQUIRE_APPROVAL. A grant cannot silently be created by a path that did not require explicit approval.

### AuthorizationRequest

Represents one request that reached the human authority boundary, preserving capability, represented subject, requester, time window, matched rule, state, correlation and provenance.

### PermissionGrant

Represents reusable authority derived from explicit approval. V0 supports the semantics NONE, ONE_TIME, TIME_BOUND and PERSISTENT; a persisted grant cannot use NONE.

## Capability Lab scenario contract

`attention_router.core.capability_lab` defines synthetic feature-acceptance scenarios. A scenario records:

- a stable scenario id;
- capability key;
- synthetic requester;
- synthetic request text/context;
- expected initial resolution;
- optional simulated human decision;
- expected grant mode;
- tags and safety flags.

The contract makes three properties non-negotiable:

- `synthetic_only = true`;
- `uses_real_personal_data = false`;
- `production_effects_allowed = false`.

A DENY or ALLOW scenario cannot pretend a human approval occurred. A pending or denied approval scenario cannot expect a permission grant.

## Implemented V0 scenarios

`tests/fixtures/capability_lab_scenarios.json` currently contains synthetic acceptance cases for:

1. restricted identity disclosure requiring approval and a one-time grant;
2. relationship-status disclosure resolved directly to DENY;
3. current-location disclosure requiring approval and a time-bounded grant.

These fixtures describe expected feature behavior only. They do not mutate runtime policy and they are not evidence that the full feature path is implemented.

## Current web lab

The engineering surface currently lives at:

`/static/control-plane.html`

The legacy path name is acceptable for V0 because it consumes Control Plane concepts; the user-facing identity is **Andy Capability Lab**.

It currently reads only protected, existing runtime surfaces:

- `/api/v1/admin/platform/operations/snapshot`
- `/api/v1/admin/policies`
- `/api/v1/admin/platform/matrix`
- `/api/v1/private/response-reviews?review_status=PENDING`
- `/api/v1/private/execution-intents?intent_status=PENDING`

The page performs no POST, PATCH or DELETE calls. Its admin credential lives only in page memory and is not written to localStorage or sessionStorage.

## What the lab should prove

The lab should progressively answer:

- Did a synthetic request map to the expected capability?
- Which rule matched and why?
- Did the request correctly ALLOW, DENY or REQUIRE_APPROVAL?
- Did approval produce the expected grant mode?
- Did a one-time grant get consumed exactly once?
- Did a time-bound grant expire at the correct boundary?
- Did revocation restore the normal approval path?
- Did tenant and represented-subject isolation remain intact?
- Can the feature be validated without a real WhatsApp conversation or production data?

Future lab tooling may include replay, state-machine visualization, rule explanation, synthetic actor selection, time travel for expiry tests, audit correlation and expected-versus-observed comparison.

## Existing human-authorization boundary

The repository already has `HumanExecutionAuthorizationRow` and durable human-approval evidence. Before adding persistent product-level authorization requests, the new `AuthorizationRequest` contract must be reconciled with that existing authority model.

The project must not create a second competing authorization system merely because the Capability Lab needs a convenient abstraction.

## Non-goals for V0

V0 does not:

- define the final Android or WhatsApp UX;
- act as the customer's main settings panel;
- persist governed capabilities;
- mutate approval rules;
- create real permission grants;
- approve a production HumanExecutionAuthorization;
- bypass trusted human authority;
- send messages or produce external effects;
- use real personal values in public fixtures;
- change runtime policy or database schema.

## Security and product invariants

1. Model output is never human approval.
2. Lab authentication is not execution authorization.
3. A real grant must be traceable to explicit authority.
4. Tenant and represented-subject scope are mandatory.
5. Public fixtures remain synthetic-only.
6. Ambiguous external effects fail closed.
7. Product clients consume server contracts rather than duplicating policy locally.
8. Engineering complexity visible in the lab must not automatically leak into customer UX.

## Next implementation boundary

The next safe step is **not persistence**. First, connect the synthetic scenario model to explainability: represent expected versus observed capability resolution without granting production authority or producing an external effect.

After that, reconcile `AuthorizationRequest` with the existing human-authorization model. Only then should we decide what new persistence, if any, is actually required.
