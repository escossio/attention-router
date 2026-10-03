# Personal Context V2D — Semantic Episode Builder V0

Status: implementation candidate  
Tracking: #223  
Depends on: V2A (#220), V2C (#222)  
Base: `main@e79b72000d95f75bde313e7ca9accd627164cbd6`

## 1. Purpose

V2D groups low-level events into durable semantic situations without deleting,
rewriting or hiding the original evidence.

Target:

`events/evidence -> deterministic episode -> evolving episode state`

An episode is an organizational cognitive structure.

It is not an execution instruction, authority grant or factual replacement for
its member evidence.

## 2. Existing primitives reused

V2D reuses:

- `TimelineEventRow`;
- `ConversationMessageRow`;
- `RelationshipRow`;
- `ResourceRow`;
- V2A Cognitive Graph;
- V2C confirmed identity aliases.

Raw event/message rows remain immutable source evidence.

## 3. V0 episode contract

An episode records:

- tenant;
- episode type;
- deterministic semantic key;
- lifecycle state;
- confidence;
- sensitivity;
- started-at;
- last-activity-at;
- optional ended-at;
- provenance;
- structural scope;
- supersession / split / merge lineage.

Lifecycle states:

- ACTIVE
- CLOSED
- SUPERSEDED
- SPLIT
- MERGED

V0 never silently deletes a prior episode.

## 4. Episode membership

Membership is first-class.

A membership records:

- episode id;
- member type;
- member ref;
- association reason;
- confidence;
- source;
- observed-at;
- ambiguity flag;
- metadata.

V0 member types:

- TIMELINE_EVENT
- CONVERSATION_MESSAGE

V0 association reasons:

- EXACT_RESOURCE
- EXACT_RELATIONSHIP
- EXPLICIT_THREAD
- EXPLICIT_SEMANTIC_KEY

A membership is idempotent.

The same evidence cannot be added twice to the same episode.

## 5. Deterministic V0 builders

### 5.1 Resource-scoped episodes

Events that:

- belong to one tenant;
- reference the same exact resource;
- match the requested episode type;
- fall inside a bounded temporal gap;

may be grouped into one resource-scoped episode.

The semantic key is deterministic from:

`tenant + episode_type + resource_id + anchor window`

No similarity model is required.

### 5.2 Relationship-scoped episodes

Events that:

- belong to one tenant;
- reference the same exact relationship;
- match the requested episode type;
- fall inside a bounded temporal gap;

may be grouped into one relationship-scoped episode.

The semantic key is deterministic from:

`tenant + episode_type + relationship_id + anchor window`

## 6. Temporal boundary

V0 uses a bounded maximum inactivity gap.

If the next event is farther than the configured gap from the episode's last
activity, a new episode is created.

This prevents one recurring relationship from becoming one immortal episode.

The gap is part of the deterministic builder contract.

## 7. Sensitivity

Episode sensitivity is at least as restrictive as the most restrictive member
known to the builder.

V0 ordering:

`NORMAL < PRIVATE < SECRET`

SECRET evidence never becomes more broadly retrievable merely because it was
grouped into an episode.

The Cognitive Graph excludes SECRET episodes by default.

## 8. Ambiguity

V0 deterministic builders create non-ambiguous memberships only when exact
structural signals exist.

Ambiguous candidate membership is represented explicitly and is not selected by
the deterministic builder.

Future LLM/embedding association belongs to Candidate Insight / Graph
Intelligence and must remain proposal-only.

## 9. Lineage

Episode evolution must preserve history.

Supported lineage metadata includes:

- supersedes episode;
- split-from episode;
- merged-from episode ids.

V0 supplies persistence and explicit transition helpers.

No lineage operation deletes member evidence.

## 10. Cognitive Graph projection

V2D adds:

- `EPISODE` CognitiveNode kind;
- episode -> member graph edges;
- episode -> resource structural edge when resource-scoped;
- episode -> relationship structural edge when relationship-scoped.

Only non-SECRET episodes are projected by default.

## 11. Safety invariants

- raw evidence remains untouched;
- episode != fact;
- episode != authority;
- episode != recommendation;
- episode != execution intent;
- tenant isolation is absolute;
- cross-tenant membership is forbidden;
- exact structural scope is required in deterministic V0;
- replay is idempotent;
- no unbounded grouping;
- no automatic destructive merge;
- SECRET remains fail-closed.

## 12. Non-goals

V2D does not implement:

- LLM episode clustering;
- embeddings;
- GNN;
- semantic Candidate Insight consolidation (#224);
- graph-aware Context Compiler ranking (#225);
- obligation/expectation semantics (#226).

## 13. Acceptance

V2D is complete when:

- episode/membership persistence exists;
- resource-scoped deterministic grouping exists;
- relationship-scoped deterministic grouping exists;
- bounded temporal separation creates distinct episodes;
- replay is idempotent;
- split/merge/supersession lineage is representable;
- raw evidence is unchanged;
- Cognitive Graph exposes episode structure;
- zero authority/outbound side effects occur;
- migration and all required CI gates pass.
