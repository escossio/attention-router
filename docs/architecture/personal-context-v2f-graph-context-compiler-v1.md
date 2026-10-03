# Personal Context V2F — Graph-aware Context Compiler V1

Status: implementation candidate  
Tracking: #225  
Depends on: V2A (#220); benefits from V2C–V2E (#222–#224)  
Base: `main@ef063d140b7379f6d37e3694bf306391e7653ecc`

## 1. Purpose

V2F evolves Context Retrieval V0 from lexical-only selection into a bounded,
explainable graph-aware Context Compiler.

Target:

`current stimulus -> deterministic seeds -> bounded graph expansion -> ranked paths -> compact context packet`

The compiler selects knowledge for reasoning.

It does **not**:

- grant disclosure authority;
- grant execution authority;
- make an inference true;
- mutate Personal Context;
- create an ExecutionIntent;
- enqueue outbound work.

## 2. Reuse

V2F reuses:

- Context Retrieval V0 as deterministic lexical fallback;
- V2A Cognitive Graph as the structured substrate;
- V2C identity aliases already projected into the graph;
- V2D semantic episodes and episode membership edges;
- existing Personal Context tenant/privacy controls.

No new database table or migration is required.

## 3. Compiler contract

The compiler returns one bounded `CompiledContextPacket` containing:

- tenant;
- current query/stimulus;
- retrieval method;
- deterministic seed node ids;
- selected context items;
- graph node/edge counts;
- candidate count;
- hard item budget;
- hard estimated-token budget;
- configured hop limit;
- lexical-fallback marker;
- generated timestamp.

Each selected item contains:

- cognitive node identity and source identity;
- node kind;
- label;
- confidence;
- temporal validity;
- relevance score;
- matched lexical terms inherited from its seed;
- hop count;
- explicit graph path;
- score components;
- source attributes/provenance.

Every path step identifies:

- edge id;
- from/to nodes;
- relation kind;
- semantic relation when present;
- traversal direction.

This makes the answer to:

> Why did Andy bring this information into context?

structurally inspectable.

## 4. Deterministic seed resolution

V1 uses lexical seed resolution over bounded Cognitive Graph nodes.

The query is normalized with the same deterministic vocabulary/token boundary as
Context Retrieval V0.

Seed evidence may come from:

- node labels;
- cognitive node/source kinds;
- source identifiers;
- structured node attributes.

No LLM, embedding or learned ranker is used.

The strongest bounded seed set is selected deterministically.

## 5. Bounded graph expansion

Traversal is breadth-first and starts only from selected seeds.

V1 supports:

- configurable `max_hops` from 0 through 4;
- configurable per-node fan-out cap;
- configurable seed cap;
- configurable Cognitive Graph per-kind source limit;
- optional relation-kind allow-list;
- bidirectional traversal for contextual neighborhood discovery.

The implementation never performs an all-pairs graph scan.

The graph source itself remains bounded by `limit_per_kind`.

## 6. Relevance ranking

Each reachable node receives an explainable deterministic score composed of:

- lexical affinity of the originating seed;
- hop-distance weight;
- node confidence;
- recency;
- provenance/source weight;
- node temporal-validity weight;
- path confidence/validity weight.

No component is hidden learned state.

The complete component map is returned with the selected item.

## 7. Temporal behavior

V1 does not simply erase historical context.

Current nodes/edges receive the strongest validity weight.

Expired and not-yet-effective knowledge can remain reachable when structurally
relevant, but receives a bounded lower validity score.

Recency is independently weighted from validity so recent evidence may rank
above otherwise equivalent old evidence.

## 8. Privacy and SECRET fail-closed

The Context Compiler never requests `include_secret=True`.

Before seed resolution or traversal, nodes carrying a SECRET sensitivity or
visibility are removed from the usable subgraph together with all dependent
edges.

For fields that explicitly declare sensitivity/visibility, unknown values fail
closed rather than being treated as public.

V2F also tightens the Cognitive Graph default projection so
`TimelineEvent.visibility=SECRET` is not projected when
`include_secret=False`.

SECRET episode/message behavior from V2D remains unchanged.

Retrieval relevance still does not imply disclosure permission.

## 9. Hard packet budgets

Two independent limits apply:

- maximum selected item count;
- maximum estimated serialized token count.

Token estimation is deterministic and provider-neutral. V1 intentionally avoids
binding the compiler to one model tokenizer.

An item that would exceed the remaining packet budget is skipped.

The resulting packet reports its estimated token usage.

## 10. Lexical fallback

If graph-aware seed resolution finds no seed, V2F calls the existing
`Context Retrieval V0` boundary.

The packet explicitly reports:

`retrieval_method = LEXICAL_FALLBACK_V0`

This preserves useful exact lexical behavior, including matches that exist in
claim/fact values not currently represented as graph labels/attributes.

Fallback remains bounded, tenant-scoped and SECRET-safe through the existing
Personal Context boundary.

## 11. Relationship filters

Callers may constrain traversal to selected
`CognitiveRelationKind` values.

Example:

Using only `EXPLICIT_RELATION` lets the compiler follow governed semantic
relationships such as:

`Ângelo -> OCCUPIES -> Casa 07`

without traversing event, claim or state attachment edges.

Invalid or empty filters fail closed.

## 12. Authority boundary

A selected context item means only:

> this governed knowledge is relevant enough to provide to a later reasoning
> boundary under the current compiler rules.

It never means:

> disclose this value;

or:

> execute an action;

or:

> promote this inference to fact.

Every graph-aware item is stamped with
`grants_authority = false`.

## 13. Non-goals

V2F does not implement:

- embeddings;
- vector search;
- spreading activation;
- GNN;
- obligation/expectation semantics (#226);
- attention/salience (#227);
- graph intelligence engines (#228);
- prompt injection into a specific LLM provider;
- automatic suggestion or execution.

Those remain later governed slices.

## 14. Acceptance

V2F V1 is complete when:

- graph-aware retrieval contract exists;
- seed resolution is deterministic;
- traversal is bounded by hops, fan-out and source limits;
- relationship-kind filters work;
- ranking includes temporal/confidence/provenance components;
- every selected graph item has an explanation path;
- lexical V0 fallback remains available;
- item/token budgets are hard;
- SECRET data is fail-closed;
- tenant isolation is preserved;
- compilation creates zero authority/outbound side effects;
- public and distributed CI are green.
