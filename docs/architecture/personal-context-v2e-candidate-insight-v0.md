# Personal Context V2E — Candidate Insight and Semantic Consolidation V0

Status: implementation candidate  
Tracking: #224  
Depends on: V2A (#220), V2C (#222), V2D (#223)  
Base: `main@c50f2d28142478e7e32d2b54ed5daa3a82632896`

## 1. Purpose

V2E creates the governed boundary between semantic interpretation and canonical
Personal Context knowledge.

Target:

`evidence/episodes -> CandidateInsight -> validation -> admission/rejection -> future governed promotion`

A Candidate Insight is explicitly **not** canonical truth.

It does not directly create a `FactRow`, `RelationshipRow`,
`EntityStateRow`, execution intent, outbound message or capability grant.

## 2. Why a separate CandidateInsight primitive

Existing `MemoryCandidateRow` is tied to message extraction and therefore is
not a general semantic boundary for episodes, rules, LLMs, embeddings or future
graph models.

Existing `MemoryClaimRow`, `FactRow`, `RelationshipRow` and
`EntityStateRow` represent downstream governed knowledge/state shapes and
must not be used as the first landing zone for model output.

V2E therefore introduces a source-neutral candidate layer.

## 3. Candidate contract

A candidate records:

- tenant;
- stable semantic key;
- idempotency key;
- insight type;
- proposed subject;
- predicate;
- proposed value;
- source engine;
- confidence;
- sensitivity;
- temporal scope;
- contradiction refs;
- lifecycle state;
- supersession lineage;
- decision metadata;
- provenance;
- created/updated/decided timestamps.

V0 insight types:

- `CLAIM_PROPOSAL`
- `RELATIONSHIP_PROPOSAL`
- `STATE_PROPOSAL`

V0 accepted source engines:

- `RULE`
- `LLM`
- `EMBEDDING`

The persistence schema reserves `GNN` as a future engine kind, but the V2E
runtime rejects it until the V2J learned-graph readiness gates are satisfied.
Future learned outputs still land only here.

## 4. Evidence contract

Candidate evidence is first-class and separately persisted.

Supported V0 evidence types:

- `SEMANTIC_EPISODE`
- `TIMELINE_EVENT`
- `CONVERSATION_MESSAGE`

Evidence records:

- tenant;
- candidate;
- source ref;
- independence key;
- SUPPORT or CONTRADICTION role;
- confidence;
- source-derived sensitivity;
- observed timestamp;
- provenance.

Evidence references are validated against the same tenant before a candidate
is persisted.

## 5. Deterministic consolidator V0

V2E includes `consolidate_episode_scope`.

It consumes one governed V2D semantic episode and emits a
`CLAIM_PROPOSAL` anchored to the episode's exact RESOURCE or RELATIONSHIP
scope.

The candidate carries:

- the episode itself as supporting evidence;
- every non-ambiguous episode membership as supporting evidence;
- the episode temporal interval;
- sensitivity at least as restrictive as the episode/evidence set;
- deterministic provenance identifying `EPISODE_SCOPE_V0`.

Replay of the same episode snapshot is idempotent.

This consolidator is intentionally modest. It proves the governed semantic
boundary before introducing probabilistic semantic extraction.

## 6. Lifecycle

V0 lifecycle states:

- `PROPOSED`
- `NEEDS_REVIEW`
- `ADMITTED`
- `REJECTED`
- `SUPERSEDED`

Deterministic RULE candidates without contradictions start as `PROPOSED`.

LLM, EMBEDDING and GNN candidates start as `NEEDS_REVIEW` regardless of
confidence.

A candidate with contradiction evidence also starts as `NEEDS_REVIEW`.

## 7. Admission

Admission is deliberately conservative in V0.

`ADMITTED` means:

> the owner accepted this candidate as eligible for a later governed promotion
> path.

It does **not** mean:

> this candidate has become canonical truth.

V0 admission requires the unique active owner identity and at least one
supporting evidence row.

Candidates carrying unresolved contradiction refs cannot be admitted.

No admission helper writes to canonical fact, relationship or state tables.

## 8. Rejection and owner correction

The unique active owner may reject a PROPOSED, NEEDS_REVIEW or ADMITTED
candidate.

Owner-directed supersession may replace an older candidate with a newer
candidate sharing the same semantic key while preserving history.

Sensitivity is monotonic across supersession: a replacement may become more
restrictive, never less restrictive than the superseded candidate.

This preserves the invariant:

**owner correction outranks older inference without deleting evidence.**

## 9. Learned output rule

Permanent rule:

`learned output -> CandidateInsight`

Never:

`learned output -> canonical truth -> execution`

Model confidence by itself cannot promote truth.

A future model may improve proposal quality, ranking or consolidation, but it
does not own canonical Personal Context state.

## 10. Sensitivity

Candidate sensitivity is at least as restrictive as:

- the explicitly requested candidate sensitivity; and
- every referenced evidence item.

Unknown TimelineEvent visibility fails closed to SECRET.

SECRET source evidence therefore cannot become broadly retrievable merely
because it participates in semantic consolidation.

## 11. Safety invariants

- CandidateInsight != FactRow;
- CandidateInsight != authority;
- CandidateInsight != execution intent;
- CandidateInsight != recommendation authority;
- raw evidence remains immutable;
- evidence refs are tenant-scoped;
- learned engines always start in NEEDS_REVIEW;
- unresolved contradictions block admission;
- owner correction can reject or supersede older inference;
- replay is idempotent;
- no external side effect is created.

## 12. Non-goals

V2E does not implement:

- automatic materialization into FactRow;
- automatic relationship creation;
- automatic EntityState mutation;
- Context Compiler ranking (#225);
- obligation/expectation semantics (#226);
- embedding generation;
- learned graph training;
- GNN execution.

## 13. Acceptance

V2E V0 is complete when:

- CandidateInsight and evidence persistence exist;
- deterministic episode-scope consolidation exists;
- replay is idempotent;
- learned outputs remain review-only;
- admission/rejection are owner-governed;
- contradiction handling is fail-closed;
- temporal/evidence lineage is preserved;
- supersession is non-destructive;
- zero authority/outbound side effects occur;
- migration and all required CI gates pass.
