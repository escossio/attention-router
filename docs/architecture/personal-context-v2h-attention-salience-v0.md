# Personal Context V2H — Cognitive Attention and Salience Engine V0

Status: implementation candidate  
Tracking: #227  
Depends on: V2A (#220), V2F (#225), V2G (#226)  
Base: `main@de542a51b14f48660913158882467d5d9bbde678`

## 1. Purpose

V2H decides which contextual changes deserve processing or represented-owner
attention without turning every anomaly into a notification.

Target:

`context change -> salience assessment -> IGNORE | REASONING_QUEUE | OWNER_SUGGESTION_CANDIDATE`

Attention is a relevance/governance boundary.

It is not execution authority.

## 2. Why a separate attention layer

V2F answers:

> what context is relevant to this stimulus?

V2G answers:

> what was expected, and what was observed?

V2H answers:

> is this change important enough to consume more reasoning or reach the owner
> suggestion boundary?

Without V2H, every anomaly or expectation change risks becoming noisy proactive
behavior.

## 3. Persistent assessment contract

V2H introduces `AttentionAssessmentRow`.

One assessment records:

- tenant;
- stable signal key;
- exact source snapshot fingerprint;
- source type/reference/state;
- deterministic score;
- threshold score class;
- effective class after governance;
- bounded score components;
- reason codes;
- sensitivity;
- cooldown window;
- owner suppression window/reason/actor;
- owner acknowledgment;
- supersession lineage;
- provenance;
- created/updated timestamps.

Supported V0 sources:

- OBLIGATION_INSTANCE;
- MEMORY_CLAIM_ANOMALY.

Exactly one ACTIVE assessment may exist per tenant/signal key.

Exact source snapshot replay is idempotent.

## 4. Score versus effective class

V2H deliberately separates:

`score_class`

from:

`effective_class`

The score class answers only:

> how salient is this signal under deterministic scoring?

The effective class answers:

> after owner policy, dedup and suppression, how far may this signal progress?

A highly salient signal can therefore remain internal.

## 5. Deterministic score dimensions

V0 uses ten bounded components, each in [0, 1]:

- impact;
- urgency;
- novelty;
- confidence;
- temporal proximity;
- relationship relevance;
- represented-owner relevance;
- expectation violation;
- recurrence stability;
- source quality.

Weights are fixed and sum to 1.0:

- impact: 0.14;
- urgency: 0.12;
- novelty: 0.08;
- confidence: 0.10;
- temporal proximity: 0.10;
- relationship relevance: 0.08;
- owner relevance: 0.12;
- expectation violation: 0.14;
- recurrence stability: 0.06;
- source quality: 0.06.

No learned or hidden weight is used.

## 6. Threshold classes

V0 thresholds:

- score < 0.65 -> IGNORE;
- 0.65 <= score < 0.82 -> REASONING_QUEUE;
- score >= 0.82 -> OWNER_SUGGESTION_CANDIDATE.

These thresholds are policy constants, not model predictions.

OWNER_SUGGESTION_CANDIDATE still does not send anything.

## 7. Explainability

Reason codes are derived only from bounded dimensions and control effects.

Examples:

- HIGH_IMPACT;
- HIGH_URGENCY;
- NOVEL_CHANGE;
- HIGH_CONFIDENCE;
- TEMPORALLY_NEAR;
- RELATIONSHIP_RELEVANT;
- OWNER_RELEVANT;
- EXPECTATION_VIOLATION;
- STABLE_RECURRENCE;
- STRONG_SOURCE;
- OWNER_PRIVATE_ACTIONABILITY_CAP;
- OWNER_NON_ACTIONABLE_CAP;
- DEDUP_SUPPRESSION_WINDOW;
- OWNER_SUPPRESSED;
- OWNER_ACKNOWLEDGED.

Reason codes never embed raw source values or SECRET payloads.

## 8. Obligation attention

V2H directly understands V2G ObligationInstance state.

Examples:

EXPECTED well before due is normally low value.

UNCONFIRMED_AFTER_DUE receives:

- high expectation-violation weight;
- high urgency;
- high impact;
- strong owner relevance;
- high recurrence stability for a governed monthly definition.

This makes a meaningful unconfirmed expectation eligible to reach the bounded
owner-suggestion boundary without asserting non-payment.

The V2G invariant remains:

`UNCONFIRMED_AFTER_DUE != did not pay`

## 9. Missing-step anomaly attention

V2H can also assess V1N MISSING_EXPECTED_STEP claims.

The score reuses:

- anomaly confidence;
- recency;
- expected signature kind;
- source-sequence support ratio;
- source-sequence occurrence count;
- source quality.

A stale low-value anomaly can therefore remain IGNORE even though it still
exists as historical knowledge.

Fresh, strong anomalies may remain in REASONING_QUEUE or reach the suggestion
threshold depending on the deterministic components.

## 10. Owner privacy/actionability controls

V2H reuses the existing V1R owner-policy overlay for MemoryClaim-based
attention.

If source context is owner-private or owner-non-actionable:

- scoring still occurs;
- internal reasoning may remain available;
- OWNER_SUGGESTION_CANDIDATE is capped to REASONING_QUEUE.

This preserves knowledge while preventing proactive use.

The control does not rewrite the underlying anomaly or sequence.

## 11. Dedup/cooldown

A high-salience assessment that reaches OWNER_SUGGESTION_CANDIDATE starts a
bounded six-hour cooldown.

If a new snapshot of the same signal appears inside the cooldown and does not
increase score by more than 0.15:

- raw score class remains explainable;
- effective class is capped to REASONING_QUEUE;
- reason code includes DEDUP_SUPPRESSION_WINDOW.

The system may still reason internally without repeatedly surfacing the same
owner-facing exception.

## 12. Explicit owner suppression

The authenticated unique owner may suppress one attention signal for a bounded
window of at most 30 days.

While active:

- effective class = IGNORE;
- status = SUPPRESSED;
- OWNER_SUPPRESSED reason is explicit;
- no source evidence is deleted.

Reassessment after the suppression window may reactivate the same exact source
snapshot.

Suppression is not execution authority.

## 13. Owner acknowledgment

The owner may acknowledge an assessment.

ACKNOWLEDGED stops the same exact source snapshot from reappearing at the owner
suggestion boundary.

If the source later changes and produces a new snapshot, V2H may create a new
assessment.

Acknowledgment therefore means:

> I saw this state.

It does not mean:

> permanently ignore all future changes to this signal.

## 14. Bounded suggestion boundary

`build_owner_suggestion_candidate` emits only a typed proposal object when:

- assessment is ACTIVE;
- effective class = OWNER_SUGGESTION_CANDIDATE;
- source is non-SECRET;
- no active owner suppression applies.

The proposal explicitly carries:

- requires_user_confirmation = true;
- execution_requested = false;
- grants_authority = false.

It does not persist a V1O suggestion, enqueue outbox, create an ExecutionIntent
or invoke a provider.

A later runtime integration may decide how to consume this boundary.

## 15. SECRET fail-closed

SECRET obligation instances, definitions or MemoryClaim anomalies are excluded
before an AttentionAssessment is persisted.

No SECRET source content is copied into:

- score components;
- reason codes;
- logs;
- audit payloads.

V2H V0 emits no audit/log record containing source payload.

## 16. Safety invariants

- salience != truth;
- salience != disclosure permission;
- salience != execution authority;
- high score != automatic notification;
- owner-private/non-actionable can cap proactive progression;
- suppression/ack never delete evidence;
- dedup never discards the source state;
- exact replay is idempotent;
- one ACTIVE assessment per signal;
- SECRET fails closed;
- no OutboxMessage or ExecutionIntent is created.

## 17. Non-goals

V2H V0 does not implement:

- outbound notification delivery;
- LLM-based salience scoring;
- learned thresholds;
- graph embeddings;
- spreading activation;
- GNN;
- automatic capability mapping;
- automatic remediation.

## 18. Acceptance

V2H is complete when:

- persistent salience result contract exists;
- deterministic bounded scorer exists;
- threshold classes exist;
- score/effective-class separation is explicit;
- reason codes are explainable and payload-safe;
- owner controls cap proactive actionability;
- dedup/cooldown works;
- owner suppression and acknowledgment work;
- low-value anomalies stay silent;
- meaningful V2G exceptions can reach the bounded suggestion boundary;
- SECRET data fails closed;
- zero outbound/execution side effects occur;
- migration and required CI gates are green.
