# Personal Context V1O — Proposal-Only Anomaly Suggestions

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1M governed event-sequence hypotheses
- V1N governed missing-step anomalies

## Purpose

Convert one sufficiently strong, active missing-step anomaly into a bounded
proposal to review what changed, without attaching execution semantics.

Target:

`active EVENT_SEQUENCE -> active MISSING_EXPECTED_STEP -> REVIEW_MISSING_ROUTINE_STEP suggestion`

V1O is the first proactive layer over sequence anomalies, but it deliberately
does not reuse the executable recommendation lifecycle.

## Why a separate suggestion class

The existing `context.recommendation.proactive` lifecycle is currently an
execution-aware contract for `reminder.create`.

A missing routine step does not yet justify an executable capability mapping.

V1O therefore persists:

- predicate = `context.suggestion.proactive`;
- source_quality = `DERIVED_SUGGESTION`;
- suggestion_type = `REVIEW_MISSING_ROUTINE_STEP`;
- lifecycle_state = `PROPOSED`;
- capability_name = null;
- execution_requested = false;
- grants_authority = false;
- requires_user_confirmation = true.

This prevents a proposal-only anomaly review from accidentally entering V1E,
V1J, V1F or V1K.

## Source threshold

A suggestion may be built only when:

- anomaly claim is ACTIVE, non-secret and unexpired;
- anomaly type = MISSING_EXPECTED_STEP;
- anomaly remains INFERRED/HYPOTHESIS;
- anomaly confidence >= 0.65;
- exact source EVENT_SEQUENCE claim is still ACTIVE;
- source sequence confidence >= 0.80;
- source support ratio >= 0.75;
- source occurrence count >= 3;
- both source claims remain non-authoritative.

## Bounded wording

The suggestion says only that an expected routine step did not appear inside
its learned window and asks whether the owner wants to review the changed
context.

It does not claim:

- that something bad happened;
- that the user forgot something;
- that another person failed to act;
- that the routine is permanently broken;
- that an external action should be taken.

## Validity

A suggestion lives for at most 24 hours and never outlives its source anomaly.

Its stable identity is derived from the exact anomaly claim, exact source
sequence claim and suggestion type.

Exact replay is idempotent.

## Revision

V1O reconciles active suggestions before building new ones.

A suggestion is superseded when:

- its source anomaly becomes inactive; or
- its source sequence becomes inactive.

It expires when its own validity window closes.

This means late-ingested expected-step evidence can:

1. supersede the V1N absence hypothesis;
2. cause V1O to supersede the derived suggestion;
3. preserve both records as history.

## Runtime

The existing Personal Context runtime cycle now:

1. detects/persists recurrence;
2. detects/persists event sequences;
3. reconciles/detects/persists missing-step anomalies;
4. reconciles/builds/persists anomaly review suggestions;
5. continues the existing executable reminder recommendation path separately.

V1O persistence occurs even when recommendation delivery is disabled.

## Hard non-execution boundary

V1O does not create or invoke:

- capability mapping;
- recommendation ACCEPT/DISMISS lifecycle;
- ExecutionIntentRow;
- ReminderRow;
- OutboxMessageRow;
- provider call;
- FactRow.

The existing V1G delivery switch continues to apply only to the already
governed executable reminder recommendation path.

A later slice may add an explicit owner-facing delivery/lifecycle for
`context.suggestion.proactive`; V1O does not do so.

## Proof

Tests cover:

- strong active missing-step anomaly builds one bounded review suggestion;
- suggestion persists idempotently;
- capability_name remains null;
- execution_requested/grants_authority remain false;
- persistence creates no execution/reminder/outbox/fact side effect;
- late expected-step evidence supersedes the anomaly and then its suggestion;
- runtime persists the suggestion even with delivery enabled without creating
  any delivery or execution side effect.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
