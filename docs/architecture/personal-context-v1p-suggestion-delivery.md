# Personal Context V1P — Suggestion Delivery and Non-Executable Reply Lifecycle

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1O proposal-only anomaly suggestions
- V1I explicit executable recommendation replies

## Purpose

Deliver one proposal-only Personal Context suggestion to the owner and record a
bounded explicit response without converting that response into execution or
automatic disclosure authority.

Target:

`PROPOSED anomaly-review suggestion -> delivered -> INTERESTED | DISMISSED`

V1P remains separate from the executable reminder recommendation lifecycle.

## Independent delivery gate

V1P adds:

`PERSONAL_CONTEXT_SUGGESTION_DELIVERY_ENABLED=false`

The flag is OFF by default and independent from:

- `PERSONAL_CONTEXT_RECOMMENDATION_DELIVERY_ENABLED`;
- `PERSONAL_CONTEXT_AUTHORITY_RUNTIME_ENABLED`;
- `PERSONAL_CONTEXT_MATERIALIZATION_RUNTIME_ENABLED`.

This allows suggestion delivery to be rolled out separately from executable
recommendations.

## Delivery contract

Only an ACTIVE, non-terminal `context.suggestion.proactive` claim produced by
V1O may be enqueued.

The owner delivery binding follows the existing Personal Context rule:

1. exactly one PRIMARY_OWNER_WHATSAPP binding wins;
2. otherwise exactly one eligible active owner binding is required;
3. ambiguity fails closed.

The outbox action is:

`personal_context_suggestion_text`

The message explicitly offers these deterministic reply forms:

- `quero revisar`
- `não quero revisar`

and states that the reply executes no action.

## Reply vocabulary isolation

V1P intentionally does not use bare `sim` / `não`.

This prevents collision with the V1I reminder recommendation reply parser.

Examples:

- `quero revisar` -> INTERESTED
- `quero ver o contexto` -> INTERESTED
- `me mostre o contexto` -> INTERESTED
- `não quero revisar` -> DISMISSED
- `ignorar sugestão` -> DISMISSED
- `sim` -> not a suggestion reply
- `não` -> not a suggestion reply

An active owner-control clarification still retains precedence over Personal
Context replies.

## Delivery correlation

A reply resolves only when:

- inbound event is authenticated OWNER_COMMAND;
- from_me/self-chat classification is intact;
- an active owner binding matches the exact external actor;
- the suggestion is ACTIVE + PROPOSED + unexpired;
- matching suggestion outbox is DONE;
- suggestion ID + claim ID match the delivered outbox;
- external actor matches the delivered outbox;
- delivery completed before the reply;
- exactly one eligible delivered suggestion exists.

Zero candidates means the message is not consumed as a suggestion reply.

Multiple candidates fail closed as ambiguous.

## Lifecycle

The terminal owner response is stored as a new versioned MemoryClaim snapshot.

INTERESTED means only:

`the owner explicitly expressed interest in reviewing this context`

It does not mean:

- permission to execute;
- permission to create a reminder;
- permission to send a message;
- permission to reveal sensitive/private context;
- a capability grant;
- an approval token.

The resulting snapshot preserves:

- capability_name = null;
- execution_requested = false;
- grants_authority = false.

DISMISSED likewise remains non-executable.

## Source revalidation

Before accepting a reply, V1P revalidates that both:

- source missing-step anomaly;
- source EVENT_SEQUENCE

are still ACTIVE and unexpired.

If the source chain changed after delivery, the reply is consumed safely as
belonging to the stale suggestion but the lifecycle does not transition to
INTERESTED/DISMISSED.

The owner receives a bounded confirmation that the suggestion is no longer
valid and that nothing will be executed or automatically revealed.

## Transport boundary

V1P reuses the existing local transport outbox dispatcher.

Successful delivery records:

`personal_context.suggestion_delivered`

The suggestion outbox itself has no execution_intent_id.

## Hard safety boundary

Neither suggestion delivery nor reply resolution creates:

- ExecutionIntentRow;
- AgentExecutionIntentRow;
- ReminderRow;
- FactRow;
- capability request;
- capability grant;
- provider execution.

An owner-control confirmation may be enqueued after a reply, exactly as with
other authenticated owner-control acknowledgements.

## Proof

Tests cover:

- persisted suggestion is not delivered when suggestion delivery is disabled;
- enabled delivery creates one deterministic suggestion outbox;
- delivery text uses non-ambiguous explicit reply phrases;
- delivered `quero revisar` -> INTERESTED;
- delivered `não quero revisar` -> DISMISSED;
- INTERESTED creates no execution/reminder/fact authority;
- reply before delivery cannot resolve the suggestion;
- bare `sim` / `não` are not suggestion replies;
- V1I recommendation parser does not consume V1P reply phrases;
- source invalidation after delivery blocks lifecycle transition;
- worker forwards the independent suggestion delivery flag.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
