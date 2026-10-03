# Personal Context V2G — Obligation and Expectation Model V0

Status: implementation candidate  
Tracking: #226  
Depends on: V2A (#220), V2E (#224), V2F (#225)  
Base: `main@fdadfc2f493efaae0d35eb4fecb65a0fd85d06a7`

## 1. Purpose

V2G makes EXPECTED state first-class.

Target:

`governed recurring definition -> bounded expectation instance -> observed evidence -> reconciliation`

Canonical example:

`Ângelo -> RENT -> Casa 07 -> due day 10 -> 2026-10 instance -> expected RENT_PAYMENT`

The central semantic invariant is:

**UNCONFIRMED_AFTER_DUE != non-payment.**

Absence is an inference about evidence coverage, never a factual accusation.

## 2. Persistence model

V2G introduces four durable primitives.

### RecurringObligationDefinition

Represents the governed recurring rule:

- tenant;
- subject type/id;
- obligation kind;
- expected actor;
- expected event type;
- optional value constraints;
- cadence;
- due day;
- due timezone;
- bounded grace period;
- confidence;
- sensitivity;
- source kind/reference;
- provenance;
- temporal validity;
- supersession lineage;
- lifecycle state.

V0 supports:

- RESOURCE or RELATIONSHIP subject;
- MONTHLY cadence;
- due days 1 through 28.

The restricted V0 keeps recurrence deterministic and avoids ambiguous
end-of-month semantics.

### ObligationInstance

Represents one bounded period of one recurring definition.

It contains:

- definition;
- period key;
- period start/end;
- expected-by time;
- due-window end;
- current state;
- reconciliation status;
- uncertainty code;
- expected value;
- satisfaction ratio;
- sensitivity;
- extension/waiver state;
- provenance.

States:

- EXPECTED;
- SATISFIED;
- PARTIALLY_SATISFIED;
- EXTENDED;
- WAIVED;
- UNCONFIRMED_AFTER_DUE;
- SUPERSEDED.

### ObligationFulfillment

Links an observed TimelineEvent to an expectation instance.

It records:

- exact event ref;
- fulfillment fraction;
- observed value;
- whether event sharing was explicitly governed;
- reconciliation kind.

One event may not silently satisfy multiple obligations.

Cross-instance reuse fails closed unless the caller explicitly sets the governed
shared-event path.

### ObligationTransition

Append-only transition evidence for instance lifecycle changes.

It records:

- from/to state;
- reason code;
- owner decision actor/reference when relevant;
- evidence ref when relevant;
- metadata;
- timestamp.

The current instance row is convenient state.

The transition ledger preserves how that state was reached.

## 3. Governed definition creation

A definition may currently originate from:

- OWNER_DECLARED;
- ADMITTED_CANDIDATE.

OWNER_DECLARED requires the unique active owner and an explicit decision ref.

ADMITTED_CANDIDATE requires an existing V2E CandidateInsight in ADMITTED state,
inside the same tenant and on the same subject.

Candidate sensitivity propagates monotonically into the definition.

Candidate confidence caps the promoted definition confidence.

No CandidateInsight is silently promoted.

## 4. Definition correction

If an active definition with the same semantic identity already exists, a
different schedule/value snapshot must explicitly name the definition it
supersedes.

The old definition becomes SUPERSEDED and remains durable.

The replacement receives `supersedes_definition_id`.

Owner authority is required for supersession.

History is not rewritten.

## 5. Monthly instance generation

V0 generates one deterministic instance per:

`definition_id + YYYY-MM`

Generation is idempotent.

The due day is interpreted in the definition's IANA timezone.

Expected-by means the end of that local due day.

Grace is bounded and is capped before the next monthly period to prevent one
instance's open window from silently overlapping the next instance.

Generated instances begin:

- state = EXPECTED;
- reconciliation_status = PENDING;
- uncertainty_code = AWAITING_CONFIRMING_EVIDENCE;
- satisfaction_ratio = 0.

The transition ledger receives INSTANCE_GENERATED.

## 6. Event reconciliation

An observed event may fulfill an instance only when:

- same tenant;
- non-SECRET evidence;
- exact expected actor;
- exact expected event type;
- exact subject resource/relationship;
- event occurred inside the instance period.

Reconciliation is serialized with a row lock on PostgreSQL.

Fractions must be greater than zero and no greater than one.

Accumulated fractions may not exceed 1.0.

A partial observation yields PARTIALLY_SATISFIED.

Full cumulative fulfillment yields SATISFIED.

Every reconciliation writes an append-only transition with the exact
TimelineEvent ref.

## 7. Explicit multi-obligation reconciliation

The same TimelineEvent cannot silently satisfy multiple instances.

Default behavior:

`event already linked elsewhere -> fail closed`

A caller may explicitly opt into shared reconciliation.

That path is persisted as:

- `explicitly_shared = true`;
- reconciliation_kind = EXPLICIT_SHARED.

This keeps many-to-one evidence reuse visible and auditable.

## 8. EXPECTED versus OBSERVED

When the effective due window closes with zero confirming evidence:

- state becomes UNCONFIRMED_AFTER_DUE;
- reconciliation_status becomes UNCONFIRMED;
- uncertainty_code becomes NO_CONFIRMING_EVIDENCE_AFTER_DUE.

The transition metadata explicitly records:

- `absence_is_fact = false`;
- `asserts_non_payment = false`.

The system therefore says:

> I do not currently have confirming evidence.

It does not say:

> the person did not pay.

## 9. Partial evidence after due

If an instance has some confirming evidence but remains incomplete after its
due window, it stays PARTIALLY_SATISFIED.

Its uncertainty becomes:

`PARTIAL_EVIDENCE_AFTER_DUE`

The partial evidence is not discarded, and the missing portion is not silently
converted into a factual debt assertion.

## 10. Late evidence

An UNCONFIRMED_AFTER_DUE instance remains revisable.

If matching evidence is later ingested, reconciliation may move the instance
to SATISFIED.

The transition ledger then records:

`LATE_EVIDENCE_CONFIRMED`

and reconciliation status becomes:

`LATE_CONFIRMED`

The historical UNCONFIRMED transition remains intact.

This preserves temporal truth:

- at time A the system lacked confirmation;
- at time B new evidence resolved the uncertainty.

## 11. Extension

The unique active owner may extend an open obligation instance.

Extension:

- requires explicit owner decision ref;
- must move the effective deadline forward;
- must remain inside the bounded monthly period;
- writes an EXTENDED transition;
- changes no historical evidence.

When the extended window closes with no confirming evidence, the same
UNCONFIRMED_AFTER_DUE semantics apply.

## 12. Waiver

The unique active owner may waive an unsatisfied instance.

WAIVED is an explicit owner decision, not an inference.

The transition ledger records OWNER_WAIVED_OBLIGATION.

A waived instance is excluded from overdue inference.

## 13. Cognitive Graph projection

V2G extends the symbolic graph with:

Node kinds:

- OBLIGATION;
- EXPECTATION.

Relation kinds:

- OBLIGATION_SUBJECT;
- OBLIGATION_EXPECTED_ACTOR;
- EXPECTATION_DEFINITION;
- EXPECTATION_FULFILLMENT_EVENT.

Example:

`Ângelo -> EXPECTED_ACTOR -> RENT obligation -> HAS_EXPECTATION_INSTANCE -> 2026-10 expectation`

and:

`expectation -> FULFILLMENT_EVENT -> payment event`

This makes V2G immediately consumable by the V2F Context Compiler.

No graph edge grants authority.

SECRET obligation/expectation material is excluded from the default graph.

## 14. Context Compiler interaction

V2F recognizes RECURRING_OBLIGATION and OBLIGATION_INSTANCE as governed
source types for provenance scoring.

A query seeded on a person can therefore traverse into the person's expected
obligations and current expectation instances.

This is the first explicit bridge from:

`who/what is this about?`

to:

`what was expected to happen now?`

## 15. No hidden execution

V2G creates no:

- OutboxMessage;
- ExecutionIntent;
- Reminder;
- provider call;
- collection action;
- accusation;
- automatic notification.

Expectation state is knowledge.

V2H will later decide whether an expectation change deserves attention.

Authority remains a separate boundary.

## 16. Non-goals

V2G V0 does not implement:

- automatic extraction of arbitrary commitments from free text;
- debt collection;
- notification policy;
- salience/attention scoring;
- financial settlement provider integration;
- cross-tenant evidence fusion;
- learned obligation prediction;
- GNN inference.

## 17. Acceptance

V2G is complete when:

- first-class recurring definition exists;
- first-class instance exists;
- monthly generation is deterministic and idempotent;
- full and partial reconciliation work;
- extensions and waivers are owner-governed;
- overdue absence remains explicit uncertainty;
- late evidence revises uncertainty non-destructively;
- one event cannot silently satisfy multiple obligations;
- Cognitive Graph exposes obligation/expectation context;
- SECRET and tenant boundaries fail closed;
- no authority/outbound side effects are created;
- migration and required CI gates are green.
