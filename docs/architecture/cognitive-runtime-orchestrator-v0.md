# Cognitive Runtime Orchestrator V0

Tracking: #249  
Parent milestone: #248  
Certified base: `main@4d93986b2662ebcdff262843f8ddd8f095b52c41`

## Purpose

Operationalize already-merged cognitive engines against real organic evidence
without introducing new intelligence semantics.

The first implementation intentionally starts with the safest automatic path:

`ORGANIC Cognitive Graph -> deterministic V2I relation candidate -> V2E CandidateInsight`

This runtime is candidate-only.

## Runtime controls

All controls are default-off/bounded:

- `COGNITIVE_RUNTIME_ENABLED=false`
- `COGNITIVE_RUNTIME_INTERVAL_SECONDS=300`
- `COGNITIVE_RUNTIME_TENANT_LIMIT=50`
- `COGNITIVE_RUNTIME_GRAPH_LIMIT_PER_KIND=200`
- `COGNITIVE_RUNTIME_CANDIDATE_LIMIT=24`

The worker owns commit/rollback. Cognitive engines do not commit internally.

## Organic-only graph input

Live cognitive candidate generation must not learn from synthetic/canary evidence.

`build_cognitive_graph_slice` therefore accepts an optional
`timeline_lineage` filter. Cognitive Runtime V0 requests exactly
`ORGANIC`, which requires a matching canonical event lineage.

Timeline evidence without a governed canonical ORGANIC lineage is excluded
fail-closed from this live candidate path.

The default Cognitive Graph API remains unchanged when no lineage filter is
provided.

## Automatic V0 behavior

For each bounded ACTIVE tenant:

1. build an ORGANIC-only, SECRET-excluding Cognitive Graph slice;
2. run the existing deterministic V2I relation-candidate engine;
3. persist each supported result through
   `persist_relation_candidate_insight`;
4. rely on the V2E deterministic idempotency key for replay safety.

Outputs remain `source_engine=RULE` CandidateInsight rows.

## Deliberately not automatic yet

### Semantic episodes

V2D requires a semantic `episode_type`. Its canonical examples are
higher-level situations such as a property matter or job-interview process.

The runtime does not invent `episode_type = TimelineEvent.event_type`.
Episodes enter live orchestration only after an explicit semantic policy or
already-declared episode coordinate exists.

### Entity resolution

V2C provides governed proposal/decision primitives but no generic automatic
candidate generator. V0 does not fabricate one.

### Context Compiler

V2F remains consumer/query driven. Continuous compilation without a request is
not meaningful work.

### Obligation/attention

V2G/V2H are compatible with runtime orchestration for already-governed
obligations/anomalies. They are intentionally deferred from the first commit so
bounded selection and temporal refresh semantics can be reviewed independently.

## Permanent boundaries

Cognitive Runtime V0 never:

- admits/rejects CandidateInsight;
- materializes RelationshipRow from inference;
- creates execution/disclosure authority;
- sends outbound messages;
- enables embeddings/GNN/learned runtime;
- treats inferred knowledge as canonical fact.

## Replay

The first runtime path needs no new processing ledger because V2E already
enforces tenant-scoped deterministic CandidateInsight idempotency.

If a later stage cannot prove replay safety from its existing schema, that
stage must stop and introduce the smallest explicit checkpoint rather than
relying on worker timing.
