# Personal Context V1H — Governed Owner Correction

Status: implementation candidate

Related:
- #84 Personal Context V1
- V1A–V1G

## Purpose

Allow an authenticated owner correction to invalidate an inferred temporal
recurrence without promoting the correction into execution or disclosure
authority.

## Contract

An owner correction is accepted only from an authenticated `OWNER_COMMAND`
bound to the same tenant and actor.

The correction:

- supersedes the current `DERIVED_PATTERN` hypothesis snapshot;
- is persisted in the existing `MemoryClaimRow` model;
- uses predicate `context.pattern.owner_correction`;
- uses source quality `USER_DECLARED`;
- preserves the exact inbound correction event as provenance;
- remains private, perishable and bounded to at most 30 days;
- never grants authority and never marks a recommendation executable.

## Anti-resurrection

The recurrence detector may continue to observe the same historical evidence,
but persistence fails closed while an active correction exists for the stable
`hypothesis_id`.

This prevents a runtime cycle from silently recreating an inference the owner
has explicitly corrected.

## Derived-chain invalidation

Recommendation acceptance does not detach a recommendation from its source
hypothesis.

Before V1E prepares authority, the exact `source_claim_id` must still be:

- present;
- owned by the same actor;
- `DERIVED_PATTERN`;
- `ACTIVE`;
- `INFERRED`;
- `HYPOTHESIS`;
- non-authoritative;
- unexpired.

If the source was corrected or otherwise invalidated, V1E returns
`SOURCE_INVALIDATED` and creates no `ExecutionIntentRow`.

V1F reuses that fresh V1E assessment. Therefore a previously PREPARED/FROZEN
intent is retired before provider execution if its source hypothesis is no
longer current.

## Boundaries

V1H does not:

- parse arbitrary natural-language corrections;
- create a new profile store;
- create `FactRow`;
- create recommendations;
- create reminders;
- call providers;
- enable Personal Context runtime flags;
- deploy runtime changes.

## Proof

Tests cover:

- authenticated correction supersedes an inferred hypothesis;
- exact correction replay is idempotent;
- an unbound actor fails closed;
- the same hypothesis cannot be recreated during the correction window;
- an invalidated source cannot prepare an execution intent;
- a PREPARED intent is retired before reminder materialization after source
  invalidation.
