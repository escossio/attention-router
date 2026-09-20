# Personal Context V1F — Governed Recommendation Materialization

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1e-authority-revalidation.md

## Purpose

Materialize one accepted recommendation into a durable reminder only after a second current-authority revalidation.

Target chain:

`PREPARED ExecutionIntentRow
-> current authority revalidation
-> FROZEN semantic scope
-> generic capability resolver revalidation
-> reminder provider
-> MATERIALIZED ExecutionIntentRow`

A PREPARED or FROZEN intent is inert.

## Input boundary

V1F accepts only the V1E semantic scope:

- schema_version = `personal-context-recommendation-execution-v1`;
- exact tenant/person;
- exact recommendation and accepted recommendation claim;
- exact acceptance event;
- capability = `reminder.create`;
- exact capability version;
- bounded `summary` + `trigger_at`;
- exact policy ID/version;
- exact active grant IDs;
- exact provider instance;
- authority_result = `ALLOW`;
- approval_required = false.

The stored scope fingerprint and provenance must match the scope exactly.

Any PREPARED scope tampering fails closed.

## First revalidation

Before freezing, V1F calls the V1E authority evaluator again.

The same exact intent ID must still be the currently valid `INTENT_PREPARED` result.

If policy, grant, capability version, provider or approval state changes such that another intent/no intent is produced, the original intent is retired and no reminder is created.

Expected blocked outcomes include:

- policy unresolved/denied;
- capability grant missing;
- provider/capability unavailable;
- current approval required;
- authority scope changed.

## Freeze

Only a still-current PREPARED intent may transition to:

`FROZEN`

The semantic fields are not modified.

PostgreSQL's existing frozen-intent immutability trigger remains authoritative.

A FROZEN intent may be retried after an interrupted materialization attempt, but must pass the same fresh revalidation again.

## Runtime provider boundary

V1F currently supports only the canonical internal reminder runtime:

- capability: `reminder.create`;
- provider instance canonical name: `internal:scheduler`;
- interface: `InternalSchedulerProvider`.

V1F does not auto-provision or repair provider state.

An unexpected provider runtime fails closed.

## Second revalidation

After freeze, V1F invokes the existing generic `execute_capability` path with:

- `owner_authorized = false`;
- `approval_granted = false`;
- current policy result;
- current actor/grant;
- a runtime registry containing only the exact current internal scheduler provider.

Therefore the generic resolver independently rechecks:

- capability/provider availability;
- current grant;
- current policy;
- current approval requirement.

A race/change between the first and second revalidation cannot silently reach the provider.

If the second resolution blocks or requires approval, the FROZEN intent becomes RETIRED and no reminder is created.

## Idempotency

The reminder provider receives:

- execution intent id;
- intent idempotency key;
- acceptance event as causation;
- actor;
- capability/provider identity.

ReminderRow uses the same ExecutionIntent idempotency key.

Therefore retry after a provider-level success cannot create a second reminder.

Calling V1F on an already MATERIALIZED intent returns the existing ReminderRow.

## Materialization

Only a successful `execute_capability` result may create a ReminderRow.

V1F validates that the returned reminder:

- exists;
- belongs to the same tenant;
- belongs to the same actor;
- uses the same intent idempotency key.

Then and only then:

`FROZEN -> MATERIALIZED`

An audit event records the recommendation, intent, reminder, policy/version, grant IDs and provider instance.

## Retirement

A stale PREPARED/FROZEN intent transitions to:

`RETIRED`

with `retired_at` and an audit reason.

Examples:

- expired intent;
- grant revoked;
- policy changed;
- provider unavailable;
- current approval requirement;
- unsupported runtime.

RETIRED intents never materialize.

## Side-effect boundary

A successful V1F materialization creates:

- exactly one ReminderRow;
- canonical/timeline reminder events already owned by the scheduler provider;
- audit evidence.

It does **not** create:

- OutboxMessageRow;
- AgentExecutionIntentRow;
- FactRow;
- external delivery.

Reminder firing remains the existing internal scheduled-event path and explicitly performs no external delivery.

## V1F proof

Tests must prove:

- full current authority materializes exactly one reminder;
- MATERIALIZED replay is idempotent;
- revoked grant retires the intent before reminder creation;
- policy deactivation retires before reminder creation;
- provider degradation retires before reminder creation;
- expired intent retires before reminder creation;
- PREPARED scope tampering fails closed;
- accepted recommendation still does not mutate to execution authority;
- no outbox, AgentExecutionIntentRow or FactRow is created by V1F.
