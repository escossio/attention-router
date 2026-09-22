# Pending Intent V1 — Contract

Status: active runtime implementation; provider-neutral provenance extended for native Client Command

Related:
- #84 Personal Context V1
- #89 Intent Clarification V1
- #90 User Idiolect V1
- PR #91 personal-language architecture

## 1. Core boundary

A Pending Intent represents **unresolved human meaning**.

It is not:
- an execution intent;
- a human execution authorization;
- a queued action;
- a capability grant;
- a response review.

The central separation is:

`language -> semantic intent -> clarification (if needed) -> resolved semantic intent -> capability mapping -> authority -> execution`

Resolving meaning never grants authority.

## 2. Semantic intent is not capability

V1 must not force every plausible human meaning into an action that already exists.

A phrase may refer to:
- a registered executable capability;
- a known semantic intent whose capability is currently unavailable;
- ordinary/non-operational conversation.

The semantic registry must remain **closed and versioned**. The model may not invent arbitrary intents, tools or actions.

This allows Andy to say, in effect:

> I now understand what you mean, but I cannot perform that operation yet.

without silently mapping the request to a different executable command.

### Motivating example

`retorne em 30 segundos`

Plausible registered semantic meanings can include:

- `CONFIGURE_OWNER_REPLY_GRACE` — change a persistent/configured grace value;
- `ONE_SHOT_DELAYED_REPLY` — delay only the current reply once;
- `RESUME_OPERATION_AFTER_DELAY` — resume a paused operation after a duration, if/when that semantic intent exists in the registry.

Only semantic intents that map to an available registered capability can later become executable.

## 3. PendingIntent durable object

Proposed logical fields:

- `id`
- `schema_version`
- `semantic_registry_version`
- `tenant_id` — semantic/operational tenant that owns the canonical person and learned meaning
- `source_tenant_id` — tenant in which the source transport/client message was authenticated/persisted
- `person_actor_key`
- exactly one source provenance:
  - `source_inbound_event_id` + `source_interaction_id`; or
  - `source_client_command_id`
- `source_channel`
- `conversation_key_hash`
- `state`
- `ambiguity_reason`
- `candidate_set`
- `candidate_set_fingerprint`
- `clarification_outbox_id`
- `clarification_delivered_at`
- exactly one resolution provenance when resolved:
  - `resolution_inbound_event_id`; or
  - `resolution_client_command_id`
- `selected_candidate_key`
- `resolution_kind`
- `created_at`
- `expires_at`
- `resolved_at`
- `updated_at`
- `correlation_id`
- `provenance`

The original message remains canonical in its source ledger (`InboundEventRow` or `ClientCommandMessageRow`); do not duplicate an unrestricted transcript into Pending Intent. The semantic tenant and source tenant may differ only when the authenticated client bridge has already resolved one unambiguous operational owner scope.

## 4. Candidate contract

Each candidate is immutable after Pending Intent creation.

Suggested logical shape:

- `candidate_key` — opaque/stable within the Pending Intent;
- `semantic_intent_key` — entry from the closed semantic registry;
- `parameters` — validated parameters for that semantic intent;
- `semantic_fingerprint`;
- `confidence`;
- `evidence_summary`;
- `capability_mapping`:
  - `AVAILABLE`
  - `UNAVAILABLE`
  - `NOT_APPLICABLE`
- optional `capability_key` only when a registered mapping exists.

Human-facing option text is rendered from validated semantic candidates. It is not itself authority.

The full candidate set receives a deterministic fingerprint. Resolution must bind to the same frozen set.

## 5. States

V1 states:

- `PENDING` — clarification exists and may still be resolved;
- `RESOLVED` — exactly one candidate was selected with sufficient correlation;
- `CANCELED` — user explicitly declines/abandons the clarification;
- `SUPERSEDED` — a newer incompatible clarification replaces it;
- `EXPIRED` — resolution window elapsed.

Terminal states never return to PENDING.

Invalid or unrelated replies do not mutate the Pending Intent to a fake terminal success.

## 6. Active-intent constraint

Because current transports do not uniformly preserve a quoted-message/reply reference, V1 must not rely on reply-thread identity that is not actually available. WhatsApp uses its normalized conversation identity; the native Android Client Command channel derives one bounded conversation scope per authenticated Human Identity.

For the same:

`tenant + canonical person + channel + conversation_key_hash`

there may be at most **one active PENDING clarification** in the initial text-channel implementation.

A new incompatible clarification must explicitly supersede the previous one before becoming active.

This constraint can be relaxed later when exact quoted-message correlation exists.

## 7. Clarification delivery

A Pending Intent may reference one clarification outbox message.

The system records:
- outbox identity;
- delivery evidence when available;
- exact frozen candidate set associated with that prompt.

A reply cannot resolve a candidate set different from the one the user was asked about.

## 8. Response correlation

A short response has no standalone semantic authority.

The resolver must prove all applicable conditions:

1. same tenant;
2. same canonical person;
3. same source/channel authority;
4. same conversation identity;
5. exactly one active Pending Intent;
6. not expired;
7. response occurs after Pending Intent creation and is compatible with clarification chronology;
8. response can be mapped to exactly one frozen candidate, or explicitly cancel the clarification.

### Correlation strength

Prefer, in order:

1. exact quoted-message/provider reply reference to the clarification message;
2. structured option/button reference;
3. unique active Pending Intent + same conversation + bounded TTL + unambiguous resolution text.

V1 WhatsApp self-chat and the native Client Command channel currently start at level 3 because neither path exposes a stronger structured reply reference in this contract.

## 9. Bare `sim` rule

`sim` is valid only when the frozen clarification is semantically binary and the affirmative answer selects exactly one candidate.

For a question such as:

> Você quer A ou B?

`sim` resolves nothing and must not select either candidate.

For a question such as:

> Você quer dizer A?

`sim` may select A only if the Pending Intent correlation checks pass.

Likewise:
- `não` may cancel/reject a single proposed interpretation;
- `a segunda`, `opção 2`, or equivalent may select a unique indexed candidate;
- ambiguous resolution text causes another clarification or leaves the intent pending; it never guesses.

## 10. Resolver behavior

Incoming authenticated owner text first checks for a matching Pending Intent before entering normal owner-command execution.

The resolution engine is constrained to:
- the frozen candidate set;
- cancellation;
- unresolved.

It cannot introduce a new executable action during resolution.

If the user's response supplies genuinely new semantic information that does not map to the frozen set, V1 should cancel/supersede the current Pending Intent and run the new utterance through normal interpretation, potentially producing a new candidate set.

## 11. Expiry

Pending Intent must have a short, policy-defined TTL.

Expiry exists because conversational deixis such as:
- `isso`
- `essa parada`
- `a segunda`
- `sim`

loses reliable meaning as conversation moves forward.

Expiry:
- is evaluated using server time;
- is explicit/auditable;
- never auto-selects a candidate;
- does not create execution authority.

Exact TTL remains a configuration decision; the contract requires bounded lifetime, not a hard-coded duration.

## 12. Re-entry after resolution

When exactly one candidate is RESOLVED:

1. materialize the selected semantic intent from the frozen candidate;
2. map it to a registered capability/action, if available;
3. run all existing deterministic validation;
4. rebuild/verify current authority from current evidence;
5. execute only if current authority permits.

Do **not** cache or carry execution authority from the time the clarification was created.

Meaning may remain valid while authority has changed.

If the selected semantic intent has no available capability, close semantic resolution successfully but return a bounded `capability unavailable` response with no execution attempt.

## 13. Current owner-control seam

Current `main` already has:

`OwnerControlSemanticOutput.classification = MATCHED | UNRESOLVED | AMBIGUOUS`

and already turns:
- AMBIGUOUS;
- non-high-confidence MATCHED

into:

`CONTROL_COMMAND_NEEDS_CLARIFICATION`

Today `services._handle_owner_control_command()` treats that reason as terminal `REJECTED` and enqueues an error response.

V1 insertion point:

`CONTROL_COMMAND_NEEDS_CLARIFICATION`
-> create Pending Intent
-> enqueue clarification
-> return without mutation

All other rejection codes remain terminal unless a future explicit policy marks them clarifiable.

## 14. Semantic interpreter evolution

The current interpreter emits one action or AMBIGUOUS without candidate details.

V1 therefore requires a closed candidate-bearing semantic result.

Two valid implementation shapes are allowed:

### A. Extend the existing structured semantic output
Add validated `candidates[]` for AMBIGUOUS/clarification-required results.

### B. Keep the current interpreter and invoke a second narrow candidate-builder
Only after `CONTROL_COMMAND_NEEDS_CLARIFICATION`, generate a closed candidate set.

The architectural requirement is the same:
- candidate keys come from a closed semantic registry;
- parameter schemas are deterministic;
- candidate validation happens before persistence;
- arbitrary model-generated actions are rejected.

Implementation choice should favor the smallest regression surface.

## 15. Idiolect evidence handoff

A RESOLVED Pending Intent may emit one governed language-learning evidence record for #90.

Evidence must reference:
- original inbound event;
- resolution inbound event;
- selected semantic intent/referent;
- contextual scope;
- direction `USER_TO_ANDY_LANGUAGE`;
- evidence class `EXPLICITLY_CONFIRMED`;
- confidence justified by the exact resolution mechanism.

This evidence improves future interpretation but never bypasses #89 clarification policy or capability authority.

## 16. Concurrency and isolation invariants

- tenant isolation mandatory;
- canonical person identity mandatory;
- active Pending Intent lookup must be scoped by tenant/person/conversation;
- candidate set is immutable;
- terminal transitions are idempotent;
- concurrent resolutions may produce at most one RESOLVED winner;
- stale replies cannot resolve a newer Pending Intent;
- cross-tenant/cross-person/cross-conversation replies fail closed.

Use row locking/uniqueness appropriate to PostgreSQL implementation when runtime work begins.

## 17. Audit vocabulary

Minimum semantic events:

- `intent_clarification.created`
- `intent_clarification.prompt_enqueued`
- `intent_clarification.prompt_delivered`
- `intent_clarification.resolution_received`
- `intent_clarification.resolved`
- `intent_clarification.unresolved_reply`
- `intent_clarification.canceled`
- `intent_clarification.superseded`
- `intent_clarification.expired`
- `intent_clarification.capability_unavailable`

Audits should store identifiers/fingerprints and reason codes, not unnecessary full sensitive text.

## 18. Required V1 tests

### Semantic
- ambiguous interpretation produces at least two valid closed candidates;
- unsupported/unavailable semantic intent can coexist with an executable candidate;
- invented semantic registry key is rejected;
- candidate parameter mismatch is rejected;
- candidate set fingerprint is deterministic.

### Correlation
- unique valid same-conversation reply resolves;
- cross-tenant reply fails;
- cross-person reply fails;
- expired reply fails;
- response to superseded Pending Intent fails;
- ambiguous `sim` does not choose between A/B;
- yes/no single-candidate clarification can resolve;
- duplicate resolution is idempotent;
- two concurrent resolutions cannot both win.

### Authority
- no state mutation occurs at clarification creation;
- no state mutation occurs from an unresolved reply;
- resolution does not bypass current authority validation;
- authority revoked during clarification prevents later execution;
- unavailable capability produces no execution attempt.

### Idiolect
- successful explicit resolution emits bounded provenance-bearing evidence;
- canceled/expired/unresolved clarification emits no confirmed-meaning evidence;
- evidence remains tenant/person scoped.

## 19. Example: `retorne em 30 segundos`

Desired conceptual flow:

1. authenticated owner self-chat arrives;
2. deterministic parser does not uniquely resolve;
3. semantic layer finds material alternatives, for example:
   - persistent grace configuration = 30 s;
   - one-shot delayed reply = 30 s;
4. Pending Intent is created with frozen candidates;
5. Andy asks which meaning is intended;
6. owner selects one;
7. Pending Intent becomes RESOLVED;
8. if selected intent maps to a capability, existing authority is evaluated **now**;
9. only then may mutation/execution occur;
10. explicit resolution may become #90 idiolect evidence.

This is the behavior that prevents a linguistically plausible but materially wrong silent action.
## 14. Native Client Command bridge

The authenticated Android Client Command channel reuses the same closed semantic registry, clarification resolver, Pending Intent lifecycle and User Idiolect projection. It must not maintain a second clarification engine or manufacture synthetic WhatsApp/inbound events.

For a native command:

`ClientCommandMessageRow -> semantic interpretation -> candidate set -> PendingIntent -> human resolution -> canonical semantic intent -> authority/execution -> confirmed idiolect evidence`

The source command remains in the client-command ledger. `PendingIntent` stores only bounded provenance IDs and the frozen candidate set. A confirmed meaning is recorded against the semantic/operational tenant and canonical owner, while provenance continues to point to the authenticated source tenant and client command.
