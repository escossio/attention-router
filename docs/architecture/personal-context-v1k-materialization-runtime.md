# Personal Context V1K — Materialization Runtime Orchestration

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1E authority revalidation
- V1F governed materialization
- V1J authority runtime orchestration

## Purpose

Move governed Personal Context execution intents from the inert V1J boundary
into the existing V1F materialization boundary.

Target:

`PREPARED/FROZEN Personal Context ExecutionIntent -> V1F -> MATERIALIZED | RETIRED`

For the current bounded capability this may create one internal
`ReminderRow(status=SCHEDULED)`.

## Independent runtime gate

V1K adds:

- `PERSONAL_CONTEXT_MATERIALIZATION_RUNTIME_ENABLED=false`
- `PERSONAL_CONTEXT_MATERIALIZATION_INTENT_LIMIT=50`

Both remain safe by default because the runtime gate is OFF.

V1K is independent of the V1G detector/delivery flags and the V1J authority
runtime flag. This permits staged rollout and recovery of already-prepared
intents without coupling unrelated Personal Context stages.

The cycle reuses `PERSONAL_CONTEXT_RUNTIME_INTERVAL_SECONDS`.

## Candidate boundary

V1K selects only intents that are:

- state `PREPARED` or `FROZEN`;
- idempotency-key namespace `personal-context:`;
- provenance origin `PERSONAL_CONTEXT_RECOMMENDATION`.

The intent scope must identify the exact tenant, actor and recommendation under
the V1E Personal Context execution schema.

Malformed candidates fail closed and are audited.

## Materialization boundary

V1K does not implement capability execution itself.

Every candidate is passed to:

`materialize_prepared_recommendation_execution`

Therefore all V1F guarantees remain authoritative:

1. exact semantic-scope/fingerprint validation;
2. fresh V1E authority revalidation;
3. source-hypothesis validation;
4. current policy/version validation;
5. current grant validation;
6. current capability/provider validation;
7. approval-state validation;
8. FROZEN transition before provider invocation;
9. second generic capability-resolver evaluation;
10. idempotent InternalSchedulerProvider materialization.

If any current authority condition no longer holds, the intent is retired and
no reminder is created.

## Transaction isolation

The worker executes V1K after V1J in a separate transaction boundary.

Failure in V1K cannot roll back:

- normal worker work;
- V1G hypothesis/recommendation work;
- V1J authority assessment or PREPARED intent creation.

Inside V1K each intent is materialized under its own savepoint. One malformed or
blocked intent does not abort other eligible intents.

## Concrete effect boundary

V1K is the first Personal Context runtime slice that may create a persistent
effect record: `ReminderRow`.

That reminder is still internal scheduler state.

The existing scheduler may later transition a due reminder to `FIRED` and
record canonical/timeline evidence. That scheduler path explicitly uses
`external_delivery=False`.

V1K does not create:

- OutboxMessageRow;
- AgentExecutionIntentRow;
- external transport calls;
- text/audio/call delivery;
- FactRow.

## Replay and recovery

After successful materialization, the intent becomes `MATERIALIZED` and is no
longer selected by V1K.

V1F retains idempotency against the ExecutionIntent idempotency key, and it also
supports bounded recovery of a valid `FROZEN` intent.

## Proof

Tests cover:

- PREPARED intent -> one MATERIALIZED intent + one SCHEDULED ReminderRow;
- subsequent cycle creates no duplicate reminder;
- revoked grant retires the intent before materialization;
- invalidated source hypothesis retires the intent before materialization;
- due reminder firing remains internal and creates no outbox/agent execution;
- materialization runtime flag is OFF by default;
- V1K can be gated independently from V1G/V1J;
- interval and intent-count bounds remain enforced;
- repository gates and distributed-postgres exact-SHA validation pass.

## Runtime

No migration.

No deployment.

No feature flag is enabled by this change.
