# Personal Context V2C — Cross-source Entity Resolution V0

Status: implementation candidate  
Tracking: #222  
Depends on: V2A (#220) and V2B (#221)  
Base: `main@3f1372cb66c567abce64a9277987d704597dcea7`

## 1. Purpose

V2C introduces governed cross-source identity resolution.

The problem is not "find similar names".

The problem is:

`do these source observations refer to the same real-world person/entity?`

Canonical example:

`WhatsApp "Ângelo" + contract "Angelo Silva" + bank descriptor "ANGELO SILVA"`

V2C must never silently collapse those observations into one identity.

It creates evidence-backed candidates, explicit decisions and reversible alias
lineage.

## 2. Existing primitives reused

V2C reuses:

- tenant-scoped `ActorBindingRow`;
- canonical `actor_key`;
- `MemoryActorRow`;
- Personal Context evidence/provenance discipline;
- V2A Cognitive Graph person nodes.

Existing ActorBinding already solves a narrower case:

`provider external identity -> canonical actor_key`

V2C solves the broader case where two already-existing actor keys may represent
the same real person.

## 3. V0 scope

V0 is intentionally person/actor focused.

A candidate compares two distinct actor keys inside one tenant.

The pair is canonicalized deterministically so replay and source-order changes
cannot create duplicate candidate identities.

V0 does not mutate `ActorBindingRow.actor_key`.

Historical source evidence is preserved exactly as observed.

## 4. Resolution candidate

A candidate records:

- tenant;
- deterministic actor-key pair;
- evidence fingerprint;
- confidence;
- state;
- evidence count;
- decision lineage;
- timestamps.

States:

- PROPOSED
- CONFIRMED
- REJECTED
- AMBIGUOUS
- SUPERSEDED

A rejected candidate remains durable.

The exact same pair + exact same evidence fingerprint returns the existing
candidate instead of reproposing it.

## 5. Identity evidence

Evidence is first-class and immutable.

Each evidence row records:

- evidence type;
- stable source reference;
- confidence;
- independence/dependency key;
- bounded metadata;
- timestamp.

V0 supported evidence types:

- EXACT_PROVIDER_IDENTITY
- NORMALIZED_PHONE
- NORMALIZED_EMAIL
- OWNER_CONFIRMATION
- SHARED_STABLE_IDENTIFIER
- EXPLICIT_RELATIONSHIP
- SOURCE_ALIAS

A display-name match alone is deliberately not an admissible identity proof.

V0 may store display names in bounded metadata for explanation, but
`DISPLAY_NAME_ONLY` is not a supported evidence type.

## 6. Independence

Two observations are not automatically two independent confirmations.

Evidence therefore has an `independence_key`.

Evidence with the same independence key belongs to the same evidence family.

This lets later scoring avoid double-counting copied/derived evidence.

V0 stores that structure explicitly even though it does not yet implement a
complex Bayesian fusion model.

## 7. Confirmation and alias lineage

Confirmation requires explicit owner-confirmation lineage in V0.

A confirmed candidate creates a durable alias-resolution record:

`alias_actor_key -> canonical_actor_key`

The alias record:

- preserves both actor keys;
- preserves the source candidate;
- records decision provenance;
- is reversible;
- does not rewrite historical ActorBinding rows.

Alias states:

- ACTIVE
- REVOKED
- SUPERSEDED

Only ACTIVE alias rows are projected into the Cognitive Graph.

## 8. Rejection and ambiguity

REJECTED means:

"with this evidence set, do not merge these identities."

AMBIGUOUS means:

"the current evidence is insufficient or conflicting."

Both fail closed.

Neither state modifies ActorBinding, MemoryActor or source evidence.

## 9. Cognitive Graph projection

V2C extends the read-only Cognitive Graph with explicit governed alias edges.

Example:

`person:whatsapp-angelo -> IDENTITY_ALIAS -> person:canonical-angelo`

The edge is derived only from an ACTIVE confirmed alias-resolution row.

The original person nodes remain present.

The graph therefore gains canonical navigation without erasing provenance.

## 10. Safety invariants

- tenant isolation is absolute;
- cross-tenant candidates are forbidden;
- actor pair members must already exist inside the tenant;
- same actor key on both sides is invalid;
- one matching display name cannot auto-confirm;
- LLM/embedding/GNN output is not confirmation;
- candidate creation grants zero authority;
- confirmation grants zero execution authority;
- historical source rows are never rewritten;
- rejected evidence replay is idempotent;
- alias revocation is reversible;
- owner-confirmed resolution is knowledge, not action authority.

## 11. Non-goals

V2C does not implement:

- learned similarity;
- embeddings;
- GNN;
- arbitrary organization/resource fusion;
- Episode Builder (V2D);
- Candidate Insight semantic consolidation (V2E);
- Context Compiler ranking (V2F).

## 12. Acceptance

V2C is complete when:

- candidate/evidence/alias persistence exists;
- candidate replay is deterministic;
- confirm/reject/ambiguous lifecycle exists;
- confirmation requires owner lineage;
- aliases are reversible;
- cross-tenant resolution fails closed;
- source evidence remains unchanged;
- Cognitive Graph exposes ACTIVE aliases;
- no execution/outbound side effects occur;
- migration and all required CI gates pass.
