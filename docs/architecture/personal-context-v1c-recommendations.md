# Personal Context V1C — Bounded Recommendation Contract

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1a-recurrence.md
- docs/architecture/personal-context-v1b-hypothesis-persistence.md

## Purpose

Derive one explainable proactive opportunity from an active governed hypothesis without executing any capability.

V1C proves:

`persisted hypothesis -> bounded recommendation -> human decision`

It does not implement:

`recommendation -> execution`.

## Initial recommendation rule

V1C has one intentionally narrow deterministic mapping:

- active `TEMPORAL_RECURRENCE`
- event_type = `LOCATION_ARRIVAL`
- sufficiently strong evidence
- registered usable capability = `reminder.create`

Result:

> Andy may propose preparing a reminder for the next predicted occurrence.

No other pattern type or event type is generalized into a recommendation in V1C.

## Evidence threshold

Recommendation requires:

- ACTIVE, non-expired `MemoryClaimRow`;
- predicate = `context.pattern.temporal_recurrence`;
- source_quality = `DERIVED_PATTERN`;
- evidence_class = `INFERRED`;
- hypothesis_status = `HYPOTHESIS`;
- grants_authority = false;
- recommendation_ready = false on the source hypothesis;
- confidence >= 0.80;
- support_ratio >= 0.75;
- occurrence_count >= 3.

The recommendation layer performs its own stricter readiness test instead of trusting the hypothesis to mark itself ready.

## Capability boundary

The recommendation must map to an existing capability definition and current version.

For V1C:

- capability = `reminder.create`;
- capability must be in a usable registered availability state;
- its current input contract must require `summary` and `trigger_at`.

The recommendation builder does not call:

- capability resolution;
- grant evaluation;
- executor;
- provider;
- scheduler;
- outbox;
- delivery.

Capability existence is necessary for a recommendation, but it is not execution authority.

## Recommendation object

`ContextRecommendation` includes:

- stable recommendation_id;
- tenant/person scope;
- source hypothesis claim id;
- recommendation_type;
- status = `PROPOSED`;
- human-readable explanation;
- registered capability name and availability;
- suggested parameters;
- confidence/support/occurrence/anomaly evidence summary;
- provenance classes;
- generated_at;
- valid_until;
- requires_user_confirmation = true;
- execution_requested = false;
- grants_authority = false.

## Prediction

For a temporal recurrence, the next expected occurrence is derived deterministically from:

- last observed event;
- cadence_seconds;
- current time.

The proposal expires no later than the next predicted occurrence or source hypothesis expiry.

## Privacy

The human-readable explanation does not dump raw timeline events, coordinates, private messages, or internal identifiers.

It may summarize only bounded evidence:

- recurrence count;
- approximate cadence;
- that a recurring arrival was observed.

Internal provenance remains in the structured recommendation object.

## Safety invariant

Calling `build_context_recommendations` must not create or mutate:

- ReminderRow;
- OutboxMessageRow;
- AgentExecutionIntentRow;
- FactRow;
- capability grants;
- provider state;
- external delivery.

A recommendation is knowledge plus a proposal, not authority.

## V1C proof

Tests must prove:

- a strong active recurrent arrival can produce one explainable recommendation;
- recommendation maps to registered `reminder.create`;
- suggested trigger is the next predicted occurrence;
- same source state produces stable recommendation identity;
- absent capability blocks recommendation;
- low confidence/support blocks recommendation;
- expired source blocks recommendation;
- unrelated recurrence remains hypothesis-only;
- tenant/person isolation is fail-closed;
- generation creates zero execution/delivery side effects.

Future slices may persist recommendation lifecycle state or accept/reject a proposal, but execution remains a separate authority boundary.
