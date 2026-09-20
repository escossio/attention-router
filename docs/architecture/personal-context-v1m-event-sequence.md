# Personal Context V1M — Governed Event-Sequence Hypotheses

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1A deterministic temporal recurrence
- V1B governed hypothesis persistence
- V1H governed owner correction
- V1L relationship-scoped recurrence

## Purpose

Extend Personal Context from repetition of one scoped event to a bounded,
explainable two-step routine hypothesis.

Target:

`scoped event A -> scoped event B -> repeated at least 3 times -> EVENT_SEQUENCE hypothesis`

Example:

`LOCATION_DEPARTURE(home) -> LOCATION_ARRIVAL(gym)`

V1M is inference only. It does not add a recommendation or execution mapping.

## Detection boundary

V1M considers only adjacent scoped TimelineEvent rows for the same tenant and
owner/person.

A step is scoped by the existing precedence:

1. RESOURCE
2. RELATIONSHIP
3. explicit event_ref.pattern_key

The two steps must be semantically distinct.

The allowed intra-sequence gap is bounded to:

- minimum: 1 minute;
- maximum: 6 hours.

This intentionally trades recall for explainability. V1M does not run
unrestricted all-pairs correlation over the timeline.

## Stability

A candidate sequence requires at least three occurrences.

The detector computes the median A→B gap and accepts occurrences inside a
bounded tolerance:

- at least 15 minutes;
- at most 90 minutes;
- otherwise 25% of the median gap.

The hypothesis preserves:

- occurrence count;
- anomaly count;
- support ratio;
- confidence;
- median A→B gap;
- first/last observed time;
- 14-day expiry;
- exact TimelineEvent transition pairs;
- exact source provenance set.

## Multi-source evidence

The two events and different occurrences may come from different normalized
sources.

Examples include device/location, calendar, WhatsApp, SMS or future
integrations after they have been normalized into TimelineEventRow.

Raw provider payloads are never correlated directly.

## Persistence

V1M uses the existing MemoryClaimRow model with:

- predicate: `context.pattern.event_sequence`;
- source_quality: `DERIVED_PATTERN`;
- pattern_type: `EVENT_SEQUENCE`;
- evidence_class: `INFERRED`;
- hypothesis_status: `HYPOTHESIS`;
- sensitivity: PRIVATE;
- perishable expiry;
- grants_authority = false;
- recommendation_ready = false.

No FactRow is created.

Exact replay is idempotent and changed snapshots supersede prior snapshots
without deleting history.

## Owner correction

V1H owner correction is generalized to support both:

- `context.pattern.temporal_recurrence`;
- `context.pattern.event_sequence`.

A correction records the exact corrected predicate and pattern type.

While the correction is active, historical sequence evidence cannot silently
recreate the hypothesis.

The sequence may requalify before correction expiry only after at least three
complete A→B occurrences where both events happened after the correction.

This preserves the rule:

`owner correction > old inference`

while still allowing deterministic relearning from genuinely new evidence.

## Runtime

The existing V1G Personal Context cycle now runs both:

- temporal recurrence detection/persistence;
- event-sequence detection/persistence.

The aggregate hypothesis counters include both classes, with additional
sequence-specific counters for observability.

V1M adds no new runtime flag and inherits the existing default-off
`PERSONAL_CONTEXT_RUNTIME_ENABLED` boundary.

## Non-actionability

V1M adds no recommendation mapping for EVENT_SEQUENCE.

The existing V1C recommendation builder reads only
`context.pattern.temporal_recurrence`, so an event sequence cannot silently
produce:

- proactive recommendation;
- ExecutionIntent;
- ReminderRow;
- outbox;
- provider call.

A future sequence-to-capability mapping must be explicit and independently
governed.

## Proof

Tests cover:

- three irregularly spaced A→B occurrences create one deterministic sequence;
- two occurrences are insufficient;
- tenant/person evidence never mixes;
- exact transition pairs and multi-source provenance are persisted;
- replay is idempotent;
- sequence remains inferred and non-authoritative;
- owner correction invalidates the sequence;
- old evidence cannot resurrect the corrected sequence;
- V1G persists sequence knowledge without creating recommendation or outbox.

## Runtime state

No migration.

No deploy.

No feature flag is enabled by this change.
