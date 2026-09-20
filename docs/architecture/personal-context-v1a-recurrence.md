# Personal Context V1A — Deterministic Recurrence Hypothesis Contract

Status: implementation candidate

Related:
- #84 Personal Context V1

## Purpose

Detect explainable temporal recurrence over canonical `TimelineEventRow` evidence without turning the result into fact, authority, recommendation, or execution.

V1A is deliberately read-only.

## Evidence boundary

Inputs are tenant/person-scoped timeline events.

A timeline event participates only when it has one bounded signature:

- `resource_id`; or
- `event_ref.pattern_key`.

Unscoped generic events such as repeated messages are not generalized into user patterns.

## Recurrence rule

A hypothesis requires at least three events in one stable cadence chain.

Cadence candidates are derived deterministically from observed time deltas.

A bounded tolerance allows ordinary timing jitter.

The detector prefers:
1. the longest supported recurrence chain;
2. the lowest normalized timing error;
3. the shorter cadence when support/stability tie.

One anomalous event may remain outside the supporting chain without rewriting the recurrence.

## Output contract

A `ContextPatternHypothesis` contains:

- tenant_id;
- actor_id;
- pattern_type = `TEMPORAL_RECURRENCE`;
- event/signature identity;
- evidence_class = `INFERRED`;
- status = `HYPOTHESIS`;
- confidence;
- cadence_seconds;
- occurrence_count;
- anomaly_count;
- support_ratio;
- first/last observed;
- valid_until;
- exact TimelineEvent evidence IDs;
- source provenance.

## Authority boundary

A V1A hypothesis:

- does not persist a FactRow;
- does not persist a MemoryClaimRow;
- does not create a recommendation;
- does not invoke a capability;
- does not grant disclosure authority;
- does not grant execution authority.

`grants_authority = false` and `recommendation_ready = false` are explicit in the contract.

## Freshness

Hypotheses are perishable.

The validity window is derived from cadence, bounded between 3 and 30 days after the latest supporting observation.

Stale recurrence is excluded from active output.

## Isolation

Evidence never crosses:

- tenant;
- canonical actor/person.

The same pattern key in another tenant or for another person is separate evidence.

## V1A proof

Tests must prove:

- three stable occurrences produce one hypothesis;
- two occurrences do not;
- multiple provenance sources are preserved;
- tenant/person evidence never mixes;
- one anomaly does not rewrite a stable recurrence;
- stale recurrence expires;
- unscoped repeated events are not generalized.

Future slices may persist governed hypotheses or derive recommendations, but must preserve these boundaries.
