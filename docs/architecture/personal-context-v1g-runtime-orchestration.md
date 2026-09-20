# Personal Context V1G — Runtime Orchestration and Proactive Delivery

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1f-governed-materialization.md

## Purpose

Move the Personal Context A→C pipeline from manually invoked application functions into the existing worker runtime, while preserving the human-decision and execution boundaries.

V1G runtime responsibilities are limited to:

`timeline evidence
-> recurrence detection
-> governed hypothesis persistence
-> bounded recommendation derivation
-> recommendation lifecycle persistence
-> proactive delivery enqueue`

V1G does **not** accept a recommendation and does **not** execute it.

## Feature gates

The worker integration is disabled by default.

Controls:

- `PERSONAL_CONTEXT_RUNTIME_ENABLED=false`
- `PERSONAL_CONTEXT_RUNTIME_INTERVAL_SECONDS=300`
- `PERSONAL_CONTEXT_RECOMMENDATION_DELIVERY_ENABLED=false`
- `PERSONAL_CONTEXT_RUNTIME_OWNER_LIMIT=50`

Detection/persistence and external channel delivery have separate switches.

Merging V1G therefore cannot begin proactive production messaging by itself.

## Worker integration

The existing `attention_router.infrastructure.worker` remains the only worker loop.

No second daemon or scheduler is introduced.

The Personal Context cycle:

1. runs at a bounded interval rather than every worker poll;
2. runs after the worker's normal transaction has committed;
3. commits in its own transaction;
4. rolls back its own work on failure;
5. cannot undo completed TTS/outbox/timer/memory work;
6. failure is logged without killing the main worker.

## Owner discovery

Runtime processing is tenant/person scoped.

A candidate owner must have an active ActorBinding where:

- actor_category = `owner`;
- binding metadata contains `owner=true`.

Multiple bindings for the same tenant/canonical actor are deduplicated before pattern evaluation.

## Pattern / hypothesis orchestration

For each owner scope, V1G calls the existing V1A detector.

Each hypothesis then crosses the existing V1B persistence boundary.

Admission, confidence, provenance, expiry and supersession rules are unchanged.

Runtime orchestration does not create a new pattern model.

## Recommendation orchestration

V1G calls the existing V1C recommendation builder.

At most one recommendation is considered per owner per cycle.

The recommendation crosses the existing V1D persistence boundary.

If the same recommendation is already the current PROPOSED lifecycle snapshot, V1G reuses it.

If the same recommendation is already terminal (ACCEPTED / DISMISSED / EXPIRED), V1G does not reopen or redeliver it.

## Delivery binding

Proactive delivery currently supports only the authenticated local WhatsApp transport path.

Eligible bindings must be:

- same tenant;
- same canonical actor;
- source = `wwebjs`;
- actor_category = `owner`;
- active;
- binding metadata `owner=true`.

Selection is fail-closed:

1. exactly one binding with `owner_channel_role=PRIMARY_OWNER_WHATSAPP` wins;
2. otherwise exactly one eligible owner wwebjs binding may be used;
3. zero or multiple ambiguous bindings block delivery.

A blocked channel does not discard the persisted recommendation.

## Interaction anchor

OutboxMessageRow requires an InteractionRow.

V1G creates a deterministic proactive interaction anchor:

- event_type = `PERSONAL_CONTEXT_RECOMMENDATION`;
- relationship_category = `owner`;
- active_context = `personal_context`;
- state = `COMPLETED`;
- bounded hashed contact identity;
- deterministic interaction ID/correlation ID from recommendation_id;
- causation = persisted recommendation claim.

This anchor bypasses the decision pipeline intentionally: the recommendation was already derived through the governed Personal Context pipeline.

## Outbox

V1G reuses the existing OutboxMessageRow + `local_transport` dispatcher.

Action type:

`personal_context_recommendation_text`

The payload contains only:

- destination external actor;
- text;
- recommendation_id;
- recommendation_claim_id.

The existing WWEBJS/local transport adapter strips internal metadata from the wire envelope and sends the normal text contract.

Outbox idempotency key is deterministic from recommendation_id.

A repeated worker cycle cannot enqueue the same recommendation twice.

## Delivery semantics

V1G delivery is deliberately non-actionable.

Example meaning:

> Andy observed a recurrence and suggests a reminder, but will not create anything without a later explicit confirmation boundary.

The V1G message does not claim that replying "yes" is already wired to the recommendation lifecycle.

Reply correlation is a later frontier.

## Delivery audit

Successful existing-outbox dispatch records:

`personal_context.recommendation_delivered`

with:

- recommendation ID;
- recommendation claim ID;
- outbox ID;
- transport status;
- provider message-reference presence.

The normal outbox completion and timeline delivery evidence also remain intact.

## Safety invariant

One V1G cycle may create:

- governed DERIVED_PATTERN MemoryClaim;
- governed DERIVED_RECOMMENDATION MemoryClaim;
- one proactive InteractionRow;
- one PENDING recommendation OutboxMessageRow.

It must create zero:

- ACCEPTED lifecycle transition;
- ExecutionIntentRow;
- AgentExecutionIntentRow;
- ReminderRow;
- FactRow;
- capability execution.

Delivery itself is only the recommendation text.

## V1G proof

Tests must prove:

- stable timeline recurrence is detected and persisted by the runtime cycle;
- one bounded recommendation is persisted;
- one deterministic interaction/outbox is created when delivery is enabled;
- repeated cycles do not duplicate pattern/recommendation/interaction/outbox;
- ambiguous owner channel blocks delivery;
- explicit PRIMARY_OWNER_WHATSAPP wins when multiple bindings exist;
- recommendation delivery uses existing process_outbox/local_transport;
- delivery produces recommendation-specific audit evidence;
- cycle creates no reminder or execution intent;
- worker feature flag and interval prevent tight-loop execution.

## Next frontier

V1H may correlate a later authenticated owner reply to exactly one delivered active recommendation.

That future slice must fail closed on ambiguous correlation and still preserve V1D's explicit lifecycle transition rules.
