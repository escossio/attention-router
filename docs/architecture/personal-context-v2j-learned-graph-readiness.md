# Personal Context V2J — Learned Graph Intelligence Readiness

Status: implementation candidate  
Tracking: #229  
Depends on: V2I (#228) and real corrected production-equivalent evaluation data  
Base: `main@c766c70ba8c156ceae0205dceaf0f113c08c960c`

## 1. Purpose

V2J implements the gate that must be satisfied before Andy introduces learned
graph intelligence such as embeddings, learned relevance rankers, link
prediction models, R-GCN/Heterogeneous GNN or temporal graph models.

V2J is intentionally **not** a GNN implementation.

The core question is:

> Do we have enough corrected, traceable, temporally separated, production-
> equivalent evidence to justify a learned model that measurably beats the
> deterministic V2I baseline?

If the answer cannot be demonstrated reproducibly, the correct state is:

`NOT_READY`

## 2. Three readiness states

V2J exposes three states.

### NOT_READY

One or more required gates are missing or failing.

Examples:

- no task policy;
- insufficient positive/negative owner labels;
- insufficient corrections;
- insufficient organic lineage;
- UNKNOWN lineage above budget;
- source quality unknown;
- insufficient temporal span;
- learned result does not beat deterministic baseline;
- split leakage;
- test set not organic;
- false-positive/calibration budget exceeded;
- explainability unavailable;
- shadow mode disabled.

### READY_FOR_OFFLINE_EVALUATION

Dataset policy gates pass, but no reproducible offline evaluation result has
been supplied yet.

This state means:

> the dataset is eligible to be evaluated for one declared task.

It does not mean:

> enable a learned model.

### READY_FOR_SHADOW_TRIAL

The task-specific dataset and offline evaluation gates pass.

This means only:

> this exact learned task/model result is eligible for a future shadow trial.

It still does not permit learned production behavior.

The report property `ready_for_learned_runtime` remains false in V2J.

## 3. No generic magic thresholds

V2J deliberately does not invent universal ML thresholds.

Every learned task must supply an explicit `LearnedGraphTaskPolicy`.

The policy declares:

- exact task;
- written justification;
- deterministic baseline name/version;
- primary metric;
- metric direction;
- minimum positive owner labels;
- minimum negative owner labels;
- minimum correction labels;
- minimum organic labels;
- minimum temporal span;
- minimum organic source quality;
- maximum UNKNOWN-lineage fraction;
- maximum UNKNOWN-source-quality fraction;
- false-positive budget;
- calibration-error budget;
- minimum improvement over deterministic baseline;
- whether temporal holdout is mandatory.

This enforces the issue requirement that each learning task be justified
individually.

## 4. Supported task names

The V2J contract recognizes:

- LINK_PREDICTION;
- NODE_ROLE_CLASSIFICATION;
- GRAPH_ANOMALY_SCORING;
- CONTEXT_RELEVANCE_RANKING;
- EPISODE_ASSOCIATION;
- TEMPORAL_NEXT_EVENT_PREDICTION.

Recognition of a task name is not task approval.

A task without a declared TaskPolicy is NOT_READY.

## 5. Owner-corrected label substrate

The readiness snapshot uses durable owner-governed decisions already present in
the V2 substrate.

Current label sources include:

### Entity resolution

- CONFIRMED owner decision -> positive label;
- REJECTED owner decision -> negative label;
- revoked alias -> correction label.

### Candidate Insight

- ADMITTED -> positive label;
- REJECTED -> negative label;
- SUPERSEDED -> correction label.

The snapshot does not treat PROPOSED or NEEDS_REVIEW Candidate Insights as
ground-truth labels.

They remain counted separately as inferred candidates.

## 6. Explicit relationship inventory

The snapshot also reports:

- total explicit RelationshipRow count;
- owner-confirmed relationship count when relationship metadata explicitly
  records owner confirmation.

This inventory is informative lineage context.

It is not silently converted into a training label if owner decision evidence
cannot be proven.

## 7. Organic versus synthetic lineage

V2J classifies label evidence as:

- ORGANIC;
- SYNTHETIC;
- UNKNOWN.

For TimelineEvent evidence, canonical-event lineage is authoritative when
available:

- ORGANIC;
- SYNTHETIC;
- UNKNOWN / HISTORICAL_UNKNOWN.

When no canonical event exists, explicit provenance/metadata is used
fail-closed.

Conversation-message and semantic-episode evidence is classified through its
source/member lineage.

Any ambiguous or missing lineage remains UNKNOWN.

UNKNOWN is never silently treated as organic.

## 8. Source quality is not confidence

A permanent V2J distinction:

**confidence != source quality**

Evidence confidence remains its existing concept.

Training/evaluation source quality must be explicitly recorded in evidence
provenance/metadata as a bounded numeric value such as:

`source_quality: 0.92`

If source quality cannot be proven, it is UNKNOWN.

A TaskPolicy may reject datasets with:

- low organic source quality;
- too many labels with unknown source quality.

V2J does not infer quality from model confidence.

## 9. Temporal validity

Label records preserve:

- decision timestamp;
- valid_from when available;
- valid_until when available.

Dataset snapshots report the temporal span of owner-decided labels.

CandidateInsight temporal validity remains part of the reproducible dataset
fingerprint.

This supports temporal evaluation without flattening all history into one
static sample.

## 10. Reproducible dataset fingerprint

Each tenant-scoped readiness snapshot computes a deterministic fingerprint from:

- tenant id;
- substrate version;
- ordered owner-decided labels;
- label state/kind;
- decision timestamp;
- temporal validity;
- lineage;
- source quality;
- explicit relationship inventory;
- evidence-type counts.

An offline evaluation result must name the exact snapshot fingerprint.

If the graph labels change after evaluation, the old evaluation no longer
matches the current dataset and the gate fails closed.

## 11. Single-tenant evaluation boundary

V2J V0 is tenant-scoped.

The readiness report and offline evaluation must refer to the same tenant.

Cross-tenant evaluation fails closed.

No cross-tenant personal-graph training architecture is introduced.

Per-user, federated, privacy-preserving aggregate or shared structural models
remain separate architecture decisions requiring explicit approval.

## 12. Offline evaluation contract

`OfflineEvaluationResult` records:

- task;
- tenant;
- exact dataset fingerprint;
- reproducibility reference;
- deterministic baseline name/version;
- primary metric;
- baseline score;
- learned score;
- false-positive rate;
- calibration error;
- train/validation/test label ids;
- train/validation/test temporal boundaries;
- temporal-holdout marker;
- organic-test-lineage declaration;
- leakage-check result;
- explainability-path availability;
- shadow-mode marker.

All metric values are finite and bounded.

## 13. Leakage prevention

Train, validation and test label ids must be individually unique and mutually
disjoint.

Every referenced label must exist in the exact dataset snapshot.

When temporal holdout is required:

- train labels must be at or before train_end;
- validation labels must be after train_end and at/before validation_end;
- test labels must be at/after test_start;
- train_end < validation_end < test_start.

This prevents a model from learning future owner corrections and then
pretending to predict them.

## 14. Organic test set

A learned model may train on a separately governed combination of data if the
TaskPolicy allows enough organic data and the future training design permits
it.

But V2J's evaluation test set must be organic when the gate says it is organic.

The code verifies both:

- the evaluation declaration;
- the actual lineage of every test label in the readiness snapshot.

Synthetic fixtures cannot certify real-world model quality.

## 15. Deterministic baseline requirement

A learned model must be evaluated against an exact deterministic baseline.

For example, a future LINK_PREDICTION policy may name:

`DETERMINISTIC_RELATION_CANDIDATE / V0`

from V2I.

The evaluation baseline name/version must exactly match the declared policy.

The learned score must strictly beat the baseline by at least the policy's
declared improvement margin.

A more complex model that merely ties the deterministic baseline is not
justified.

## 16. Calibration and false-positive budgets

TaskPolicy declares:

- max false-positive rate;
- max calibration error.

The offline result must remain under both.

These budgets are explicit task-governance inputs.

V2J does not hide them inside a model or silently relax them.

## 17. Explainability

A learned result cannot reach READY_FOR_SHADOW_TRIAL unless an explainability
path is available.

The exact explanation implementation may differ by task, but the evaluation
must prove that model output can retain usable provenance/explanation.

Learned output must still cross:

`learned output -> CandidateInsight`

Never:

`learned output -> canonical relationship/fact -> execution`

## 18. Feature flags

V2J adds two fail-closed settings:

`LEARNED_GRAPH_INTELLIGENCE_ENABLED=false`

`LEARNED_GRAPH_SHADOW_MODE=true`

Default behavior therefore has learned graph intelligence disabled.

If the feature flag is explicitly enabled, shadow mode must remain true.

Configuration with:

`enabled=true + shadow=false`

is invalid.

V2J introduces no production learned-mode configuration.

## 19. CandidateInsight GNN gate remains closed

V2J does not automatically add GNN to the application-level supported
CandidateInsight engines.

The existing V2E runtime guard remains authoritative.

A future learned-engine slice must explicitly cross this architecture boundary
after a V2J readiness result demonstrates that the task/model deserves a
shadow trial.

## 20. No persistence migration

V2J introduces no new database table.

It reuses:

- EntityResolutionCandidate/Evidence;
- EntityAliasResolution;
- CandidateInsight/Evidence;
- Relationship;
- TimelineEvent;
- CanonicalEvent lineage;
- ConversationMessage;
- SemanticEpisode/Membership.

The readiness report is a deterministic read model over already governed data.

## 21. No authority side effects

V2J creates no:

- RelationshipRow;
- FactRow;
- CandidateInsightRow;
- ExecutionIntent;
- OutboxMessage;
- provider call.

It only audits dataset/evaluation readiness.

A READY_FOR_SHADOW_TRIAL report still grants no authority.

## 22. Current architectural meaning

Completing V2J means:

- Andy has an explicit symbolic graph;
- deterministic graph intelligence exists;
- the system can measure whether learned graph intelligence is justified;
- the system can say NOT_READY honestly;
- the feature flag is default OFF;
- any first learned model must begin in shadow mode.

It does **not** mean a GNN must be built next.

If organic corrected data is insufficient, collecting better labels is the
correct next step.

## 23. Acceptance

V2J is complete when:

- task-specific readiness policy contract exists;
- owner-corrected dataset snapshot exists;
- organic/synthetic/unknown lineage is explicit;
- source quality remains distinct from confidence;
- temporal validity is represented;
- deterministic dataset fingerprint exists;
- single-tenant boundary is enforced;
- train/validation/test leakage checks exist;
- temporal holdout validation exists;
- deterministic baseline comparison exists;
- learned model must beat baseline;
- calibration and false-positive budgets exist;
- explainability is required;
- feature flag defaults OFF;
- enabled learned graph mode requires shadow mode;
- readiness can only reach READY_FOR_SHADOW_TRIAL;
- production learned runtime remains unauthorized;
- no authority/outbound side effects exist;
- public and distributed CI are green.
