# Personal Context V1G — Runtime Recommendation Pump

Status: implementation candidate

Related:
- #84 Personal Context V1
- Personal Context V1A-V1F

## Purpose

Move the proposal half of Personal Context from callable architecture into the normal worker runtime.

Each worker cycle may now advance:

`TimelineEvent evidence
→ recurrence hypothesis
→ governed hypothesis claim
→ bounded recommendation
→ PROPOSED lifecycle snapshot
→ owner delivery outbox`

V1G deliberately stops there.

It does not accept a recommendation, evaluate execution authority, create an ExecutionIntentRow, or materialize a reminder.

## Actor scope

The pump scans bounded existing MemoryActor identities.

All pattern/recommendation functions retain their existing tenant/person isolation.

## Delivery scope

A recommendation is deliverable only when exactly one eligible owner binding exists for the actor:

- active;
- actor_category = owner;
- source = wwebjs;
- binding metadata owner = true.

Ambiguous or missing delivery binding fails closed:

- the governed PROPOSED recommendation remains persisted;
- no OutboxMessageRow is created;
- an audit event records the skipped delivery.

## Outbox

New action:

`personal_context_recommendation`

Destination:

`local_transport`

Payload contains only:

- owner external actor reference;
- text;
- recommendation ID/type;
- requires_user_confirmation = true.

It contains no execution intent and no execution authority.

The existing outbox worker dispatches this action through the same local transport adapter already used for authenticated owner-control responses.

## Idempotency

Outbox idempotency key is derived from the stable recommendation ID.

A recommendation already active for the same recommendation ID is not recreated on later worker polls even though wall-clock generated_at changes.

Repeated worker polling therefore creates no duplicate proposal or delivery.

## Worker integration

The normal worker invokes the Personal Context pump in its bounded database cycle.

The pump returns only the number of newly enqueued recommendations for observability.

No network call occurs inside the pump itself.

## Safety invariant

V1G creates zero:

- ReminderRow;
- ExecutionIntentRow;
- AgentExecutionIntentRow;
- FactRow.

It never calls:

- recommendation ACCEPT/DISMISS;
- V1E authority evaluation;
- V1F materialization;
- capability executor/provider.

The user still has to make an explicit decision through the governed recommendation lifecycle.

## V1G proof

Tests must prove:

- three recurring timeline events can advance to one PROPOSED recommendation and one outbox message;
- pattern/recommendation provenance remains governed;
- replay is idempotent;
- later worker poll does not duplicate the same recommendation;
- missing delivery binding persists the proposal but creates no outbox;
- runtime pumping never advances into execution/reminder side effects.
