# Personal Context V1H — Explicit Recommendation Reply

Status: implementation candidate

Related:
- #84 Personal Context V1
- Personal Context V1G runtime orchestration

## Purpose

Wire an explicit authenticated owner reply to one already-delivered proactive recommendation.

V1H closes:

`delivered PROPOSED recommendation -> explicit owner reply -> ACCEPTED | DISMISSED`

It still does not execute the accepted recommendation.

## Closed reply parser

V1H recognizes only a small deterministic set of explicit replies.

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

- inbound event is authenticated OWNER_COMMAND;
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

A bare yes/no is not consumed by Personal Context while an owner-control clarification is active.

## Lifecycle

V1H delegates the transition to the existing V1D lifecycle resolver.

Therefore ACCEPT/DISMISS retain:

- authenticated owner provenance;
- inbound event single-use protection;
- superseding lifecycle history;
- idempotency;
- expiry checks.

## Confirmation

After a successful decision, the existing authenticated owner-control outbox sends a confirmation.

ACCEPT confirmation explicitly says permissions will still be revalidated before any reminder is created.

## Safety invariant

V1H creates zero:

- ExecutionIntentRow;
- AgentExecutionIntentRow;
- ReminderRow;
- FactRow.

ACCEPT remains:

- execution_requested = false;
- grants_authority = false.

V1E/V1F are not invoked by the reply handler.

## V1H proof

Tests must prove:

- delivered recommendation + explicit yes -> ACCEPTED;
- delivered recommendation + explicit no -> DISMISSED;
- reply before delivery cannot resolve the proposal;
- closed parser rejects unrelated text;
- ACCEPT creates no execution intent/reminder;
- owner receives a confirmation that authority will be revalidated.
