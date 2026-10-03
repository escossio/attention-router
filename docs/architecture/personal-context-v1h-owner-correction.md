# Personal Context V1H — Owner Correction and Hypothesis Invalidation

Status: implementation candidate

Related:
- #84 Personal Context V1
- Personal Context V1A–V1G
- `docs/architecture/personal-context-v1b-hypothesis-persistence.md`
- `docs/architecture/personal-context-v1d-recommendation-lifecycle.md`
- `docs/architecture/personal-context-v1g-runtime-orchestration.md`

## Purpose

V1H gives an authenticated owner a governed way to reject an inferred Personal Context pattern.

The correction is knowledge, not execution authority. It does not delete historical evidence and does not promote any inference to fact.

Target transition:

`ACTIVE inferred hypothesis -> explicit owner correction -> hypothesis suppressed -> optional relearning only from sufficient post-correction evidence`

## Authority boundary

A correction is accepted only from an `InboundEventRow` that:

- belongs to the same tenant;
- is `OWNER_COMMAND`;
- has `owner_authenticated=true`;
- is a self-chat/from-me owner command;
- resolves through an active binding to the same canonical owner actor.

The same inbound event cannot correct two different hypotheses.

A correction never grants capability, policy, approval, disclosure, or execution authority.

## Persistence model

No parallel profile store is introduced.

The inferred source remains a `MemoryClaimRow` with:

- predicate `context.pattern.temporal_recurrence`;
- source quality `DERIVED_PATTERN`;
- evidence class `INFERRED`.

The explicit correction is another `MemoryClaimRow` with:

- predicate `context.pattern.owner_correction`;
- source quality `EXPLICITLY_CONFIRMED`;
- evidence class `EXPLICITLY_CONFIRMED`;
- `grants_authority=false`;
- `recommendation_ready=false`;
- exact correction inbound-event provenance;
- `supersedes_claim_id` pointing to the corrected inferred claim.

The corrected inferred claim becomes `SUPERSEDED`; its history remains intact.

## Derived-chain invalidation

A correction invalidates still-live work derived from the corrected pattern:

1. active Personal Context recommendations sourced from that exact claim become `SUPERSEDED`;
2. pending/retry proactive recommendation outbox rows are canceled only when both recommendation ID and recommendation-claim ID match;
3. same-tenant `PREPARED` or `FROZEN` recommendation execution intents are moved to `RETIRED`.

Already materialized effects are historical reality and are not silently undone by V1H.

## Relearning rule

The correction does not disappear merely because a timer elapsed.

The same stable hypothesis identity stays suppressed until the deterministic recurrence detector produces a valid recurrence chain containing at least **three qualifying timeline observations that occurred after the correction**.

Before that threshold, persistence fails closed with:

`PATTERN_HYPOTHESIS_OWNER_CORRECTED`

Once the threshold is met:

1. the explicit correction claim becomes `SUPERSEDED`;
2. a new inferred pattern snapshot may become `ACTIVE`;
3. the new claim links back to the correction through `supersedes_claim_id`;
4. the correction remains preserved in history.

Historical evidence is not erased. New evidence is required before the system may trust the same pattern again.

## Defense in depth

V1E now revalidates that an accepted recommendation still points to an active, inferred, non-authoritative source pattern.

If the source is missing, superseded, invalid, or expired, no new execution intent is prepared.

V1F catches that failed source-authority revalidation and retires an existing inert `PREPARED`/`FROZEN` intent instead of allowing materialization.

This makes correction safety independent of the cascade path alone.

## Idempotency and isolation

- exact replay of the same correction event is idempotent;
- one correction event cannot be reused for another hypothesis;
- correction lookup is tenant/person scoped through the canonical memory actor and owner binding;
- intent retirement additionally verifies the tenant embedded in the immutable execution scope;
- outbox cancellation requires exact derived recommendation identity;
- one owner's correction cannot modify another owner's evidence.

## Explicit non-goals

V1H does not:

- parse arbitrary natural-language corrections into hypothesis IDs;
- add an Android/UI correction surface;
- enable Personal Context runtime flags;
- deploy V1G/V1H;
- create facts;
- create reminders;
- grant execution authority;
- revoke already-materialized external effects.

Those are separate boundaries.

## Proof

Tests cover:

- authenticated owner correction superseding the inferred hypothesis;
- proactive recommendation invalidation;
- pending delivery cancellation;
- no `FactRow` creation;
- exact replay idempotency;
- unauthenticated correction fail-closed behavior;
- continued suppression with only historical evidence;
- deterministic relearning after three post-correction recurrence observations;
- execution-intent retirement isolated to the same tenant;
- V1F retirement when a prepared intent's source hypothesis becomes inactive.
