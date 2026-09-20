# Personal Context V1B — Governed Hypothesis Persistence

Status: implementation candidate

Related:
- #84 Personal Context V1
- docs/architecture/personal-context-v1a-recurrence.md

## Purpose

Persist a V1A `ContextPatternHypothesis` into existing Personal Context storage without promoting inference into fact, recommendation, disclosure authority, or execution authority.

V1B reuses `MemoryClaimRow`. No parallel profile/hypothesis database is introduced.

## Admission

A hypothesis is persistable only when:

- pattern_type = `TEMPORAL_RECURRENCE`;
- evidence_class = `INFERRED`;
- status = `HYPOTHESIS`;
- confidence >= 0.75;
- support_ratio >= 0.60;
- occurrence_count >= 3;
- valid_until is still active;
- grants_authority = false;
- recommendation_ready = false.

Every referenced TimelineEvent must:

- exist;
- belong to the same tenant;
- belong to the same actor/person;
- match event type and signature;
- not be SECRET;
- match the declared provenance set.

Fail closed on any mismatch.

## Storage mapping

Persist as:

- `MemoryClaimRow.predicate = context.pattern.temporal_recurrence`
- `source_quality = DERIVED_PATTERN`
- `status = ACTIVE`
- `staleness_class = PERISHABLE`
- `object_type = JSON`

Structured value preserves:

- pattern_type;
- event_type;
- signature kind/value;
- cadence;
- evidence_class = INFERRED;
- hypothesis_status = HYPOTHESIS;
- grants_authority = false;
- recommendation_ready = false.

Context preserves:

- stable hypothesis_id;
- snapshot fingerprint;
- exact TimelineEvent IDs;
- source provenance;
- occurrence count;
- anomaly count;
- support ratio.

## History / idempotency

Replaying the exact same hypothesis snapshot is idempotent.

When the same stable hypothesis identity is reinforced or materially changes:

1. the previous active MemoryClaim becomes SUPERSEDED;
2. its historical record remains intact;
3. a new ACTIVE claim is created;
4. `supersedes_claim_id` links the new snapshot to the previous one.

Do not silently mutate old evidence history.

## Personal Context exposure

The ACTIVE hypothesis claim participates in the existing bounded Personal Context snapshot as knowledge.

The superseded historical claim remains stored but is excluded from the active snapshot.

## Authority boundary

Persisted hypothesis is still only knowledge.

V1B does not:

- create FactRow;
- mark a hypothesis authoritative;
- create a recommendation;
- request a capability;
- execute an action;
- permit disclosure.

A future recommendation layer must consume this claim through a separate boundary and preserve explicit approval/authority requirements.

## V1B proof

Tests must prove:

- a valid V1A hypothesis persists as one governed MemoryClaim;
- Personal Context exposes the active hypothesis;
- no FactRow is created;
- exact replay is idempotent;
- reinforcement supersedes while preserving history;
- low-confidence/low-support/expired hypotheses are rejected;
- authority/recommendation flags cannot be smuggled in;
- forged provenance is rejected;
- cross-person evidence is rejected.
