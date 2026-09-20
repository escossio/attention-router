# Personal Context V1J — Authority Runtime Orchestration

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1E authority revalidation
- V1F governed materialization
- V1G runtime orchestration
- V1I explicit recommendation replies

## Purpose

Move the transition from an already `ACCEPTED` Personal Context
recommendation to V1E authority assessment into the existing worker runtime.

Target:

`ACCEPTED recommendation -> fresh V1E authority assessment -> optional PREPARED intent`

V1J deliberately stops there.

## Independent runtime gate

V1J adds:

`PERSONAL_CONTEXT_AUTHORITY_RUNTIME_ENABLED=false`

The flag is OFF by default.

Authority orchestration is independent of:

- `PERSONAL_CONTEXT_RUNTIME_ENABLED`;
- `PERSONAL_CONTEXT_RECOMMENDATION_DELIVERY_ENABLED`.

This allows staged rollout. Existing accepted recommendations may be assessed
without requiring pattern detection or proactive delivery to be enabled in the
same runtime.

The authority cycle reuses:

- `PERSONAL_CONTEXT_RUNTIME_INTERVAL_SECONDS`;
- `PERSONAL_CONTEXT_RUNTIME_OWNER_LIMIT`.

## Runtime isolation

The worker runs V1J after the V1G cycle, but with a separate transaction
boundary.

A V1J failure therefore cannot roll back:

- normal decision work;
- TTS;
- outbox delivery;
- timers;
- memory ingestion;
- V1G hypothesis/recommendation work.

Within V1J, each accepted recommendation is evaluated under its own savepoint.
One malformed or rejected recommendation does not abort other owners or other
recommendations.

## Candidate selection

V1J considers only recommendation claims that are:

- ACTIVE;
- source quality `DERIVED_RECOMMENDATION`;
- lifecycle `ACCEPTED`;
- non-expired;
- bound to an active owner scope.

The exact recommendation ID is passed to the existing V1E implementation.

## Authority boundary

V1J does not invent a second authority resolver.

It calls `evaluate_accepted_recommendation_authority`, preserving V1E
semantics:

- current source hypothesis must still be valid;
- current policy and policy version are resolved;
- current capability/version/provider are resolved;
- current grant state is checked;
- current approval requirement is checked;
- `owner_authorized=false` remains unchanged.

Possible results remain bounded to V1E statuses such as:

- POLICY_UNRESOLVED;
- CAPABILITY_UNAVAILABLE;
- DENIED;
- REQUIRES_APPROVAL;
- SOURCE_INVALIDATED;
- INTENT_PREPARED.

## Retry and idempotency

Denied or unresolved ACCEPTED recommendations remain eligible on a later cycle.
This is intentional because grants, policies, capability availability and
provider health can change after the user's acceptance.

If the same authority snapshot already produced a PREPARED intent, V1E
idempotency returns the existing intent rather than creating a duplicate.

## Hard stop before materialization

V1J never calls V1F.

Therefore V1J never:

- freezes an ExecutionIntent;
- invokes a provider;
- creates a ReminderRow;
- creates an AgentExecutionIntentRow;
- creates a FactRow;
- performs external delivery.

A PREPARED intent remains inert until a separate governed materialization
boundary is explicitly introduced or invoked.

## Proof

Tests must prove:

- an authorized ACCEPTED recommendation reaches PREPARED;
- repeated cycles preserve one idempotent PREPARED intent;
- missing grant remains denied and creates no intent;
- a later grant allows a later cycle to prepare the intent;
- source invalidation fails closed;
- no ReminderRow or other execution side effect is created;
- the authority runtime flag is OFF by default;
- the authority gate can run while the V1G detection gate remains OFF;
- interval throttling remains bounded;
- repository gates and distributed-postgres exact-SHA validation pass.

## Runtime

No migration.

No deployment.

No feature flag is enabled by this change.
