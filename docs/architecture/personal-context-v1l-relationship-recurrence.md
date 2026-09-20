# Personal Context V1L — Relationship-Scoped Recurrence

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1A deterministic recurrence
- V1B governed hypothesis persistence
- V1C bounded recommendations
- V1G runtime orchestration

## Purpose

Extend the deterministic Personal Context pattern engine to recognize repeated
events tied to one canonical relationship.

Target:

`multi-source TimelineEvent evidence -> same relationship_id -> temporal recurrence hypothesis`

V1L reuses the existing canonical timeline and RelationshipRow model. It does
not introduce a parallel contact/profile store.

## Signature precedence

Timeline recurrence signatures are resolved in this order:

1. RESOURCE
2. RELATIONSHIP
3. explicit event_ref.pattern_key

A resource-scoped event therefore remains resource-scoped even when it also
carries relationship metadata.

A relationship-scoped event is identified by the canonical
`TimelineEventRow.relationship_id`.

## Multi-source correlation

Events from different normalized sources may contribute to the same recurrence
when they share:

- tenant;
- owner/person actor;
- event type;
- canonical relationship_id.

Example evidence may come from WhatsApp, calendar, SMS or another integration
after normalization into TimelineEventRow.

Raw provider payloads are never correlated directly.

## Governed persistence

A relationship-scoped recurrence is still stored as the existing
`context.pattern.temporal_recurrence` MemoryClaim with:

- source_quality = DERIVED_PATTERN;
- evidence_class = INFERRED;
- hypothesis_status = HYPOTHESIS;
- exact TimelineEvent evidence IDs;
- exact source provenance set;
- confidence;
- support ratio;
- expiry;
- grants_authority = false;
- recommendation_ready = false.

The canonical relationship ID is preserved as:

- signature_kind = RELATIONSHIP;
- signature_value = relationship_id.

Existing V1H owner correction and supersession behavior remains applicable
because the stable hypothesis identity includes signature kind/value.

## No implicit action mapping

V1L deliberately does not add a recommendation mapping for relationship-scoped
patterns.

The current V1C reminder mapping remains restricted to the previously supported
RESOURCE and PATTERN_KEY signatures.

Therefore a relationship recurrence can become useful Personal Context
knowledge without silently creating a proactive reminder or execution path.

A future capability-specific mapping must explicitly define what a relationship
pattern means before it may become actionable.

## Isolation and safety

V1L preserves:

- tenant isolation;
- actor/person isolation;
- relationship identity isolation;
- SECRET evidence exclusion;
- provenance validation;
- bounded confidence/expiry;
- inference != fact;
- knowledge != disclosure authority;
- knowledge != execution authority.

## Proof

Tests cover:

- three events for the same relationship across WhatsApp, calendar and SMS
  produce one deterministic recurrence;
- an event for another relationship does not contaminate the pattern;
- exact relationship signature and multi-source provenance are persisted;
- the claim remains INFERRED and non-authoritative;
- a relationship-scoped LOCATION_ARRIVAL claim does not enter the existing
  reminder recommendation mapping.

## Runtime

V1G automatically benefits from the detector/persistence extension when its
existing runtime flag is enabled.

No new runtime flag is added.

No migration.

No deploy.

No feature flag is enabled by this change.
