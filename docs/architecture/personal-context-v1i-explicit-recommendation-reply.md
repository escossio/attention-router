# Personal Context V1I — Explicit Recommendation Reply

Status: implementation candidate

Related:
- #84 Personal Context V1
- Personal Context V1G runtime orchestration
- Personal Context V1H governed owner correction

## Purpose

Wire an explicit authenticated owner reply to one already-delivered proactive
recommendation.

V1I closes:

`delivered PROPOSED recommendation -> explicit owner reply -> ACCEPTED | DISMISSED`

It still does not execute the accepted recommendation.

## Closed reply parser

V1I recognizes only a small deterministic set of explicit replies.

Accept examples:

- sim
- sim, pode
- pode sim
- aceito
- quero
- crie o lembrete
- pode criar

Dismiss examples:

- não / nao
- não obrigado
- dispenso
- deixa pra lá

Other text is not interpreted as a recommendation decision.

No model inference is used.

## Correlation

A reply may resolve a recommendation only when:

- inbound event is authenticated `OWNER_COMMAND`;
- from_me/self-chat classification is intact;
- canonical owner actor is known;
- recommendation lifecycle is ACTIVE + PROPOSED;
- recommendation is not expired;
- the matching proactive outbox is DONE;
- outbox recommendation ID and claim ID match;
- outbox external actor matches the inbound external actor;
- recommendation delivery completed before the reply arrived;
- exactly one delivered active proposal is eligible.

Zero candidates means the message is not consumed as a recommendation reply.

More than one candidate fails closed as ambiguous.

## Clarification precedence

An active Intent Clarification pending intent retains precedence.

A bare yes/no is not consumed by Personal Context while an owner-control
clarification is active.

## V1H source-correction interaction

V1I delegates the lifecycle transition to the V1D resolver, which now includes
the V1H current-source check.

If a delivered proposal still exists but its source hypothesis has since been
corrected or otherwise invalidated, the explicit reply is consumed as belonging
to that proposal but cannot transition it to ACCEPTED or DISMISSED.

The owner receives a bounded confirmation that the suggestion is no longer
valid because its originating context changed. No downstream parser is allowed
to reinterpret that stale yes/no as another command.

## Lifecycle and authority

Successful ACCEPT/DISMISS retains:

- authenticated owner provenance;
- inbound event single-use protection;
- superseding lifecycle history;
- idempotency;
- expiry checks.

ACCEPT remains:

- `execution_requested = false`;
- `grants_authority = false`.

V1E/V1F are not invoked by the reply handler.

## Side-effect boundary

V1I creates zero:

- ExecutionIntentRow;
- AgentExecutionIntentRow;
- ReminderRow;
- FactRow.

A successful decision may enqueue only the existing owner-control confirmation
message.

## Proof

Tests cover:

- delivered recommendation + explicit yes -> ACCEPTED;
- delivered recommendation + explicit no -> DISMISSED;
- reply before delivery cannot resolve the proposal;
- closed parser rejects unrelated text;
- ACCEPT/DISMISS create no execution intent, agent execution, reminder or fact;
- ACCEPT confirmation states that permissions are revalidated later;
- a stale yes after source invalidation is consumed safely, leaves the proposal
  PROPOSED, creates no execution side effect and is not reinterpreted as another
  command.

## Runtime

No migration.

No feature flag is enabled by V1I.

No deployment is performed as part of this change.
