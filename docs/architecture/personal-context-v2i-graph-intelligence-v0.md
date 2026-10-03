# Personal Context V2I — Graph Intelligence V0

Status: implementation candidate  
Tracking: #228  
Depends on: V2A (#220), V2E (#224), V2F (#225)  
Base: `main@c0097bf5bf4a042980a4684c3ba73915afd1fb61`

## 1. Purpose

V2I introduces a pluggable, deterministic intelligence layer over the explicit
Cognitive Graph before any learned graph model is allowed.

Target:

`Cognitive Graph -> bounded graph algorithm -> explainable score/candidate -> governed downstream boundary`

V2I does not introduce a graph database.

V2I does not introduce embeddings or a GNN.

## 2. Stable engine interfaces

The semantic interfaces are defined independently from their implementation.

V0 exposes:

- RelevanceEngine;
- StructuralSimilarityEngine;
- RelationCandidateEngine;
- AnomalyEngine;
- ContextPathRanker.

Callers depend on these contracts rather than on one algorithm.

Later embeddings, learned rankers or GNN engines may implement the same
semantic interfaces after V2J readiness gates.

## 3. Shared budget contract

All V0 engines use a bounded GraphIntelligenceBudget.

The budget controls:

- max hops;
- max fan-out;
- max processed nodes;
- max explanation paths;
- max returned candidates.

Hard maxima prevent unbounded traversal.

V0 limits are deliberately conservative.

No engine performs all-pairs traversal over an unbounded graph.

## 4. Deterministic spreading activation

V0 RelevanceEngine implements spreading activation.

Inputs:

- one tenant-scoped CognitiveGraphSlice;
- one or more visible seed node ids;
- one bounded activation score per seed.

Propagation:

- is bidirectional for contextual association;
- uses active temporal edges only;
- uses edge confidence when present;
- gives explicit edges full inference weight;
- gives structural projection edges a bounded lower weight;
- applies a fixed decay per hop;
- does not revisit a node inside one explanation path;
- stops at budget boundaries.

Output:

- node id;
- activation score;
- strongest seed;
- hop count;
- exact explanation path.

The algorithm is deterministic and provider-independent.

## 5. Structural similarity baseline

V0 StructuralSimilarityEngine compares nodes of the same CognitiveNodeKind.

Each node is represented by a set of typed neighborhood signatures:

`direction | relation kind | semantic relation | neighbor kind`

Similarity is the Jaccard score between those signature sets.

The result includes:

- compared node id;
- similarity score;
- shared signature count;
- union signature count;
- exact shared signatures.

This is a structural baseline, not semantic identity resolution.

High structural similarity never merges entities.

## 6. Context path ranking

V0 ContextPathRanker enumerates bounded simple paths between two visible nodes.

Each path is scored using:

- edge confidence;
- inference class;
- fixed hop decay.

The result preserves every path step:

- edge id;
- from/to node;
- relation kind;
- semantic relation;
- direction;
- edge weight.

This allows downstream reasoning to answer:

> Why are these two nodes considered connected?

without relying on an opaque embedding.

## 7. Rule-based link candidates

V0 RelationCandidateEngine supports one deliberately narrow rule:

**repeated TimelineEvent co-occurrence between one PERSON and one RESOURCE.**

A candidate exists only when:

- no explicit Cognitive Graph relation already connects the pair;
- at least two independent non-SECRET TimelineEvents connect the same PERSON
  and RESOURCE;
- the graph slice is tenant-scoped;
- the evidence remains visible under current privacy rules.

The output is:

- subject node;
- target node;
- relation hint = REPEATED_EVENT_COOCCURRENCE;
- bounded confidence;
- exact supporting TimelineEvent ids;
- explanation paths;
- engine/version provenance.

This is an association candidate.

It does not claim OCCUPIES, OWNS, PAYS or any other canonical semantic relation.

## 8. CandidateInsight integration

A GraphRelationCandidate may cross the existing V2E boundary through
`persist_relation_candidate_insight`.

The adapter:

1. revalidates the current visible graph;
2. recalculates the canonical deterministic candidate;
3. rejects forged/stale confidence or evidence;
4. converts only supported TimelineEvents into V2E CandidateEvidenceInput;
5. calls `propose_candidate_insight` with source_engine = RULE.

The resulting CandidateInsight is:

- insight_type = RELATIONSHIP_PROPOSAL;
- predicate = context.graph.structural_association;
- state = PROPOSED under normal RULE semantics;
- candidate-only;
- non-authoritative;
- non-executable.

No RelationshipRow is created.

No FactRow is created.

No ExecutionIntent or Outbox row is created.

## 9. Evidence compatibility

V2I does not invent a new evidence type.

CandidateInsight persistence uses only evidence already governed by V2E.

V0 link candidates use:

- TIMELINE_EVENT.

Graph-only relevance/similarity/path results remain ephemeral when no
CandidateInsight-compatible raw evidence exists.

This keeps interpretation traceable back to supported evidence.

## 10. Simple anomaly features

V0 AnomalyEngine exposes bounded structural features for one node:

- degree;
- number of relation kinds;
- number of neighbor kinds;
- low-confidence edge ratio;
- expired edge ratio;
- isolated flag.

These are features, not anomaly truth.

They may feed later scoring or CandidateInsight logic, but V0 does not persist
an anomaly merely because one feature is unusual.

## 11. SECRET and owner privacy

V2I consumes the default Cognitive Graph projection, which already excludes
SECRET and owner-private governed context.

Every V2I engine also filters explicit sensitivity/visibility markers
fail-closed.

SECRET evidence therefore cannot:

- receive activation;
- appear in explanation paths;
- support relation candidates;
- contribute to structural similarity;
- cross the CandidateInsight adapter.

## 12. Temporal semantics

Traversal and path ranking use only currently active edges.

Expired edges do not propagate activation.

Future edges do not propagate activation.

Anomaly features may still count expired incident edges as historical
structure, but do not treat future edges as expired.

## 13. Determinism

V0 uses deterministic:

- sorted node/edge order;
- fixed decay;
- fixed confidence rules;
- fixed tie breaking;
- fixed candidate ordering;
- stable CandidateInsight evidence references.

There is no random seed.

There is no provider call.

There is no LLM call.

## 14. Replaceability

`default_graph_intelligence_engines()` wires the deterministic V0 engines.

A future implementation may replace one engine independently.

Examples:

- deterministic structural similarity -> embedding similarity;
- deterministic relevance -> learned relevance ranker;
- rule link candidate -> GNN link prediction.

The caller-facing semantic contract remains stable.

Any learned engine still returns candidate/relevance output rather than
canonical truth.

## 15. GNN remains gated

V2I does not enable source_engine = GNN in CandidateInsight.

The existing V2E GNN gate remains intact.

V2J must first certify:

- stable graph semantics;
- corrected labels;
- rejected candidates;
- temporal evaluation data;
- leakage controls;
- deterministic baselines;
- calibration and false-positive budgets.

## 16. Safety invariants

- graph intelligence != canonical truth;
- structural similarity != identity equivalence;
- relevance != disclosure authority;
- relation candidate != RelationshipRow;
- anomaly feature != anomaly fact;
- high activation != execution authority;
- owner correction remains stronger than inference;
- SECRET fails closed;
- tenant boundaries are preserved;
- every persisted candidate retains raw evidence provenance.

## 17. No migration

V2I V0 introduces no new persistence table.

It reuses:

- CognitiveGraphSlice;
- CandidateInsightRow;
- CandidateInsightEvidenceRow.

No Alembic revision is required.

## 18. Acceptance

V2I is complete when:

- pluggable semantic engine interfaces exist;
- deterministic spreading activation exists;
- structural similarity baseline exists;
- rule-based relation candidates exist;
- simple anomaly features exist;
- bounded explainable path ranking exists;
- CandidateInsight integration is idempotent and evidence-backed;
- forged/stale relation candidate persistence fails closed;
- explicit canonical relations suppress redundant link candidates;
- tenant/SECRET boundaries hold;
- graph intelligence creates zero canonical/execution side effects;
- public and distributed CI are green.
