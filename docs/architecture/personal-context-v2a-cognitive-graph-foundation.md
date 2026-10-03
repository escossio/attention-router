# Personal Context V2A — Cognitive Graph Foundation

Status: implementation candidate  
Tracking: #219  
Certification basis: `main@737ca9bfc88543f486b243642b18992826a4c865`

## 1. Purpose

Personal Context V1A–V1R already proves several governed cognitive primitives:
temporal evidence, hypotheses, recurrence, event sequences, anomaly detection,
bounded suggestions, owner correction, review, controls, authority separation
and materialization boundaries.

V2 does not replace that work.

V2 begins the next layer:

`persistent evidence -> explicit world model -> context compilation`

V2A creates the first read-only Cognitive Graph contract over the structures
already present on `main`.

The graph is initially a projection, not a new source of truth.

## 2. Governance invariant

GitHub remains the source of truth.

The required direction is:

`GitHub commit/PR -> CI -> exact SHA -> AGT validation -> optional controlled deploy`

AGT is never the authoritative development source for V2.

## 3. Certified existing primitives

The following are present on the certification SHA and are reused.

### 3.1 Identity and conversation evidence

Existing:
- `ActorBindingRow`: tenant-scoped external identity -> canonical `actor_key`;
- `MemoryActorRow`: tenant-scoped memory actor;
- `ConversationThreadRow`;
- `ConversationParticipantRow`;
- `ConversationMessageRow`.

Existing historical ingestion:
- `HistoryAdapter`;
- `HistoryBackfillService`;
- resumable cursor;
- dry-run;
- deduplication;
- message archive;
- secret redaction;
- memory candidate/promotion metrics.

Conclusion:

Historical text bootstrap plumbing already exists at a backend primitive level.
V2 must not build a second archive system.

### 3.2 Memory evidence and claims

Existing:
- `MemoryCandidateRow`;
- `MemoryExtractionRunRow`;
- `MemoryClaimRow`;
- `MemoryEvidenceRow`;
- `MemoryIngestionJobRow`.

`MemoryClaimRow` already provides:
- subject actor/entity;
- predicate;
- typed object fields;
- confidence;
- sensitivity;
- source quality;
- `valid_from` / `valid_until`;
- status;
- staleness;
- supersession;
- conflict group;
- first/last observed timestamps.

Conclusion:

V2 semantic knowledge should reuse this provenance/supersession discipline
instead of introducing an ungoverned memory store.

### 3.3 Explicit world primitives

Existing:
- `ResourceRow`;
- `RelationshipRow`;
- `FactRow`;
- `EntityStateRow`;
- `TimelineEventRow`.

`RelationshipRow` already expresses:
- source entity;
- target entity;
- relationship type;
- status;
- temporal validity.

`TimelineEventRow` already expresses:
- tenant;
- actor;
- relationship;
- resource;
- event type;
- event reference;
- occurrence timestamp;
- visibility;
- provenance.

Conclusion:

The repository already contains the minimum storage primitives required to
project an explicit graph without adding a graph database.

### 3.4 Personal Context and retrieval

Existing:
- `build_personal_context`;
- `PersonalContextSnapshot`;
- active/expiry filtering;
- secret exclusion;
- owner context controls;
- `Context Retrieval V0`.

Current retrieval method is explicitly `LEXICAL_V0`.

Conclusion:

V2A does not remove lexical retrieval. Later V2 work may add graph-aware
retrieval behind a separate contract.

### 3.5 Governed pattern / expectation lineage

Existing Personal Context slices:
- V1A deterministic recurrence;
- V1B governed hypothesis persistence;
- V1C bounded recommendation;
- V1D recommendation lifecycle;
- V1E authority revalidation;
- V1F governed materialization;
- V1G runtime orchestration;
- V1H owner correction;
- V1I explicit recommendation reply;
- V1J authority runtime;
- V1K materialization runtime;
- V1L relationship-scoped recurrence;
- V1M event-sequence hypotheses;
- V1N missing expected step anomaly;
- V1O proposal-only anomaly suggestion;
- V1P suggestion delivery/lifecycle;
- V1Q bounded structural review;
- V1R owner controls.

Important existing invariant:

`absence is inference, never fact`.

Conclusion:

V2 must extend this lineage rather than silently turning graph inference into
facts or execution authority.

### 3.6 Execution / authority boundary

Existing:
- capability registry/provider boundary;
- `ExecutionIntentRow`;
- immutable/fingerprinted execution scopes;
- `HumanExecutionAuthorizationRow`;
- native client approval;
- policy/grant/provider revalidation;
- controlled materialization.

Conclusion:

The Cognitive Graph is knowledge infrastructure. It grants zero authority.

## 4. Certified gaps

Repository search and source inspection on the certification SHA did not find a
coherent implementation of the following concepts.

These are V2 work, not claims that no related primitive exists.

### 4.1 Cognitive Graph contract

Missing:
- explicit `CognitiveNode`;
- explicit `CognitiveEdge`;
- `CognitiveGraphSlice`;
- graph provenance contract;
- graph projection API.

### 4.2 Minimal ontology registry

Missing:
- canonical entity-kind registry;
- canonical relation-kind registry;
- validation of semantic graph labels;
- explicit distinction between stable canonical kinds and model-generated
  candidate labels.

### 4.3 General entity resolution

Existing actor aliases solve an important transport identity case.

Missing:
- general cross-source candidate identity matching;
- evidence-weighted merge decision;
- reversible alias/fusion lineage;
- ambiguity state;
- user-confirmable merge proposal.

Example future problem:

`WhatsApp "Ângelo" == contract "Angelo Silva" == bank descriptor "ANGELO SILVA" ?`

### 4.4 Episode construction

Missing:
- semantic grouping of many events/messages into one durable situation;
- episode membership evidence;
- episode lifecycle.

Examples:
- sale of one property;
- recurring rent matter;
- vehicle purchase negotiation.

### 4.5 Semantic bootstrap product contract

Historical backfill exists.

Missing:
- `BootstrapRun`;
- source consent/selection metadata;
- batch lifecycle;
- pause/resume/cancel product semantics;
- progress contract;
- context-yield metrics;
- multi-source bootstrap orchestration.

### 4.6 Evidence fusion

Missing:
- cross-source evidence dependency graph;
- duplicate-origin detection;
- independent-evidence weighting;
- contradiction aggregation.

### 4.7 General obligation / expectation model

V1 patterns can represent recurrence and missing sequence steps.

Missing:
- first-class obligation;
- expected actor;
- expected event;
- due window;
- fulfillment/reconciliation;
- extension/waiver;
- partial fulfillment;
- explicit `EXPECTED vs OBSERVED` contract.

### 4.8 Graph-aware Context Compiler

Missing:
- entity-seeded graph traversal;
- bounded multi-hop expansion;
- temporal relevance;
- relationship relevance;
- confidence/provenance weighting;
- graph-path explanations;
- context budget compiler.

### 4.9 Attention / salience engine

Missing:
- bounded salience scoring using impact, urgency, novelty, confidence,
  relationship, expectation violation and user relevance;
- attention threshold/lifecycle.

### 4.10 Learned graph intelligence

Missing:
- graph embeddings;
- graph similarity;
- learned link prediction;
- GNN;
- temporal GNN.

These are explicitly not required for V2A.

## 5. V2A architecture

V2A introduces a read-only projection layer.

Source-of-truth rows remain unchanged.

### 5.1 Node contract

A Cognitive Node is a typed reference to an existing persistent object.

Required fields:
- tenant;
- node id;
- node kind;
- source table/kind;
- source id;
- display label when safe;
- temporal state where applicable;
- sensitivity/visibility where applicable;
- provenance summary.

V2A node kinds are deliberately small:
- PERSON;
- ENTITY;
- RESOURCE;
- RELATIONSHIP;
- EVENT;
- CLAIM;
- FACT;
- STATE.

Future ontology growth is versioned.

### 5.2 Edge contract

A Cognitive Edge represents one explicit relation already supported by stored
data or one structural projection edge.

Required fields:
- tenant;
- edge id;
- source node;
- target node;
- relation kind;
- confidence when meaningful;
- valid-from/valid-until;
- provenance;
- inference class.

V2A does not persist inferred edges.

### 5.3 Projection rule

V2A may project:
- actor/resource relationships from `RelationshipRow`;
- event attachment to actor/resource/relationship from `TimelineEventRow`;
- claims/facts/states to their explicit subjects;
- evidence lineage as provenance metadata.

V2A may not:
- invent a relationship;
- merge identities;
- infer family structure;
- create a FactRow;
- create a MemoryClaimRow;
- create an ExecutionIntent;
- enqueue outbound work.

### 5.4 Tenant isolation

Every graph slice is tenant-scoped.

Cross-tenant node or edge projection is a hard failure.

### 5.5 Read-only safety

Building a graph slice must create zero:
- MemoryClaim;
- Fact;
- TimelineEvent;
- Recommendation;
- Suggestion;
- ExecutionIntent;
- Outbox;
- HumanExecutionAuthorization.

## 6. Why no graph database yet

V2A needs graph semantics, not a graph-database migration.

The existing PostgreSQL source tables already provide:
- stable IDs;
- temporal fields;
- relationships;
- events;
- provenance;
- tenant isolation.

A dedicated graph store may be evaluated later if traversal/scale measurements
justify it.

V2A must not introduce Neo4j/Memgraph/TigerGraph merely to make the design
look graph-native.

## 7. Why no GNN yet

GNN is a learned graph model, not the graph contract.

Before GNN, Andy needs:
- stable node/edge semantics;
- corrected entity identities;
- real relationship labels;
- temporal evidence;
- accepted/rejected hypotheses;
- user corrections;
- evaluation datasets.

V2A intentionally creates the substrate from which later learned graph models
can be trained and evaluated.

## 8. Future transition to neuro-symbolic architecture

The intended evolution is additive:

1. explicit symbolic graph;
2. deterministic traversal/inference;
3. graph-aware context compilation;
4. embeddings/similarity;
5. learned specialized models;
6. GNN / temporal graph models.

Neural outputs remain candidate insights until governed evidence promotes them.

No future learned model becomes the owner of canonical truth.

## 9. V2A acceptance criteria

V2A is complete only when:
- graph contracts exist in source;
- a bounded read-only graph projection exists;
- existing Relationship/Resource/Timeline/Claim/Fact/State primitives are
  projected without mutation;
- tenant isolation is tested;
- provenance/temporal fields are preserved;
- inference is explicitly absent from V2A;
- no authority/execution side effects are possible;
- tests pass in GitHub CI;
- no AGT validation occurs before the exact GitHub SHA exists and CI is green.

## 10. Follow-on map

- V2B — Semantic Bootstrap Run Contract
- V2C — Entity Resolution V0
- V2D — Episode Builder V0
- V2E — Candidate Insight + Semantic Consolidation
- V2F — Graph-aware Context Compiler
- V2G — Obligation & Expectation Model
- V2H — Attention / Salience Engine
- V2I — Graph Intelligence V0
- V2J+ — embeddings / learned graph models / GNN when evidence justifies them
