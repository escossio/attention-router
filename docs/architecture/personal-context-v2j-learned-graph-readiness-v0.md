# Personal Context V2J — Learned Graph Intelligence Readiness V0

Status: implementation candidate  
Tracking: #229  
Depends on: V2I (#228) and real corrected production-equivalent evaluation data  
Base: `main@c766c70ba8c156ceae0205dceaf0f113c08c960c`

## 1. Purpose

V2J defines the evidence and evaluation gates that must pass before one
specific learned graph task may enter SHADOW mode.

It is intentionally not a GNN implementation.

Target:

`corrected governed labels -> lineage audit -> temporal holdout -> deterministic baseline -> learned offline evaluation -> readiness gates -> optional SHADOW`

Default runtime remains:

`OFF`

V2J never authorizes ACTIVE learned inference.

## 2. Task-specific readiness

There is no global `GNN_READY=true`.

Readiness is evaluated per task.

V0 defines task identifiers for:

- LINK_PREDICTION;
- ENTITY_RESOLUTION;
- NODE_ROLE_CLASSIFICATION;
- GRAPH_ANOMALY_SCORING;
- CONTEXT_RELEVANCE_RANKING;
- EPISODE_ASSOCIATION;
- TEMPORAL_NEXT_EVENT.

V0 has governed label adapters only for:

- LINK_PREDICTION;
- ENTITY_RESOLUTION.

The other tasks fail closed as unsupported until equivalent corrected labels
and deterministic baselines exist.

## 3. Governed labels

### Link prediction

V0 labels come only from V2E `RELATIONSHIP_PROPOSAL` Candidate Insights with
an owner decision.

Positive:

- state = ADMITTED;
- decision_kind = OWNER_ADMITTED.

Negative:

- state = REJECTED;
- decision_kind = OWNER_REJECTED.

PROPOSED and NEEDS_REVIEW remain unreviewed hypotheses and are not labels.

SUPERSEDED candidates remain correction lineage and are counted separately.

SECRET candidates are excluded from the readiness dataset.

### Entity resolution

V0 labels come from V2C EntityResolutionCandidate rows.

Positive:

- state = CONFIRMED;
- decision_kind = OWNER_CONFIRMED.

Negative:

- state = REJECTED;
- decision_kind = OWNER_REJECTED.

PROPOSED and AMBIGUOUS are not corrected labels.

SUPERSEDED candidates remain correction lineage.

## 4. Dataset lineage

Every corrected label carries:

- lineage class;
- source-quality/evidence class;
- valid-from / valid-until temporal scope when the governed source provides it.

Lineage classes are:

- ORGANIC;
- SYNTHETIC;
- HISTORICAL_UNKNOWN.

For CandidateInsight TimelineEvent evidence, V2J resolves lineage through the
linked CanonicalEvent when available.

For entity-resolution evidence, V2J uses only explicitly declared
`lineage_classification` metadata.

Lineage aggregation is fail-closed:

- any SYNTHETIC evidence -> SYNTHETIC label;
- all evidence ORGANIC -> ORGANIC label;
- any unresolved/mixed unknown lineage -> HISTORICAL_UNKNOWN.

Synthetic and unknown labels may be inventoried.

They are never included in the V0 temporal evaluation holdout.

## 5. Dataset inventory

The readiness report records:

- total corrected labels;
- positive labels;
- negative labels;
- organic labels;
- synthetic labels;
- historical-unknown labels;
- corrected semantic groups;
- owner-confirmed labels;
- owner-rejected labels;
- superseded/correction count;
- inferred/unreviewed count;
- explicit canonical RelationshipRow count;
- organic corrected-label temporal span.

Having many raw events is not equivalent to having useful corrected labels.

## 6. Temporal holdout

V0 builds an organic-only deterministic temporal split:

- 70% train;
- 15% validation;
- 15% test.

Ordering is based on:

`decided_at, label_id`

The manifest records:

- train label ids;
- validation label ids;
- test label ids;
- excluded non-organic label ids;
- train cutoff;
- validation cutoff;
- test start;
- deterministic split fingerprint;
- group leakage flag;
- lineage leakage flag.

Readiness requires:

`train_until < validation_until < test_from`

and no semantic group may appear across multiple splits.

If one candidate group appears in train and test, readiness fails.

## 7. Deterministic baseline

A learned result is meaningless without a baseline on the exact same holdout.

Expected V0 baseline engines are:

LINK_PREDICTION:

`DETERMINISTIC_RELATION_CANDIDATE`

ENTITY_RESOLUTION:

`DETERMINISTIC_ENTITY_RESOLUTION_V0`

The evaluation payload must identify:

- task;
- engine name;
- engine version;
- precision;
- recall;
- false-positive rate;
- calibration error;
- explainability coverage;
- test example count;
- exact temporal split fingerprint;
- reproducibility reference.

The baseline and learned evaluation must use the same test-set size **and the
exact deterministic split fingerprint** produced by the generated temporal
holdout.

Matching only the number of examples is insufficient.

## 8. V0 readiness policy

V0 ships with a conservative versioned engineering policy.

Defaults:

- minimum corrected labels: 200;
- minimum positives: 50;
- minimum negatives: 50;
- minimum organic labels: 150;
- maximum unknown-lineage ratio: 5%;
- minimum organic temporal span: 30 days;
- minimum test examples: 40;
- minimum learned precision improvement over baseline: +0.02;
- maximum learned false-positive rate: 5%;
- maximum learned calibration error: 0.10;
- minimum explainability coverage: 95%;
- maximum recall regression versus baseline: 0.02.

These numbers are not declared universal ML truths.

They are explicit V0 engineering gates and therefore may only change through an
intentional versioned policy change.

An invalid policy cannot relax readiness through contradictory or negative
thresholds.

## 9. Readiness gates

The report exposes every gate independently.

Core reason codes include:

- TASK_LABEL_ADAPTER_SUPPORTED;
- MINIMUM_TOTAL_LABELS;
- MINIMUM_POSITIVE_LABELS;
- MINIMUM_NEGATIVE_LABELS;
- MINIMUM_ORGANIC_LABELS;
- UNKNOWN_LINEAGE_RATIO;
- MINIMUM_TEMPORAL_SPAN;
- TEMPORAL_HOLDOUT_PRESENT;
- TEMPORAL_ORDER_STRICT;
- GROUP_LEAKAGE_PREVENTED;
- LINEAGE_LEAKAGE_PREVENTED;
- BASELINE_METRICS_PRESENT;
- DETERMINISTIC_BASELINE_MATCHES_TASK;
- LEARNED_METRICS_PRESENT;
- EVALUATION_MATCHES_TEMPORAL_HOLDOUT;
- MINIMUM_TEST_EXAMPLES;
- LEARNED_BEATS_BASELINE_PRECISION;
- RECALL_REGRESSION_WITHIN_BUDGET;
- FALSE_POSITIVE_BUDGET;
- CALIBRATION_BUDGET;
- EXPLAINABILITY_COVERAGE;
- LEARNED_ENGINE_DIFFERS_FROM_BASELINE;
- RUNTIME_DEFAULT_OFF.

`ready_for_shadow = true` only when every gate passes.

## 10. Precision is not enough

A model that improves precision may still fail readiness.

Examples:

- false-positive rate above budget;
- unacceptable recall regression;
- poor calibration;
- low explainability coverage;
- insufficient organic labels;
- group leakage;
- synthetic contamination;
- test metrics produced on a different split.

This prevents a single attractive metric from hiding operational risk.

## 11. Runtime modes

V2J defines:

- OFF;
- SHADOW;
- ACTIVE.

Default:

`OFF`

`authorize_learned_graph_runtime_mode` behaves as follows:

OFF:

always allowed.

SHADOW:

allowed only when the exact readiness report passes.

ACTIVE:

always rejected in V2J.

A later governed slice must separately define promotion from shadow to any
active use.

## 12. CandidateInsight GNN gate remains closed

V2J readiness infrastructure does not add GNN to the application-level
`SUPPORTED_ENGINES`.

Therefore:

`propose_candidate_insight(... source_engine="GNN")`

continues to fail.

Passing readiness means only:

> this task has enough evidence and offline evaluation to permit a future
> learned engine to run in shadow mode.

It does not mean:

> learned output may be persisted or trusted automatically.

## 13. Learned runtime invariant

Permanent invariant:

`learned output -> CandidateInsight`

Never:

`learned output -> canonical truth -> execution`

Even a future shadow GNN must retain:

- engine/version provenance;
- feature/data revision;
- task identity;
- model revision;
- confidence/calibration;
- explanation path;
- exact evidence lineage.

## 14. Privacy and tenancy

V2J evaluation is tenant-scoped.

V0 does not train or evaluate a cross-tenant personal model.

No cross-tenant aggregate model is implicitly authorized.

Per-user models, federated learning, privacy-preserving aggregation and shared
structural models remain separate architecture decisions.

Synthetic/test lineage is never silently mixed into organic evaluation.

## 15. No migration

V2J V0 is a read-only readiness/evaluation layer.

It reuses existing:

- CandidateInsight decisions/evidence;
- EntityResolution decisions/evidence;
- CanonicalEvent lineage;
- Relationship inventory.

It creates no new persistence table and requires no Alembic migration.

## 16. Current expected product state

V2J may legitimately return NOT_READY in production today.

That is a successful result when the repository does not yet contain enough
organic owner-corrected labels or a reproducible learned-model evaluation that
beats the deterministic V2I baseline.

The objective is to know exactly what is missing.

Not to force a neural model into the architecture.

## 17. Safety invariants

- readiness != model activation;
- enough raw data != enough corrected labels;
- synthetic data != organic evaluation data;
- unknown lineage fails closed;
- temporal leakage fails closed;
- group leakage fails closed;
- learned precision alone is insufficient;
- false-positive budget is mandatory;
- explainability is mandatory;
- runtime default remains OFF;
- ACTIVE is forbidden by V2J;
- no authority or outbound side effects exist.

## 18. Acceptance

V2J is complete when:

- task-specific corrected label adapters exist;
- lineage is explicit and fail-closed;
- organic-only temporal holdout exists;
- split/group leakage detection exists;
- deterministic baseline identity is required;
- learned metrics must match the exact holdout;
- false-positive/calibration/explainability budgets exist;
- readiness returns explicit reason-coded gates;
- invalid readiness policies fail closed;
- OFF is default;
- only readiness-gated SHADOW can be authorized;
- ACTIVE remains forbidden;
- CandidateInsight GNN source remains gated;
- zero canonical/execution side effects occur;
- public and distributed CI are green.
