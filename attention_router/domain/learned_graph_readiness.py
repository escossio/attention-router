"""Contracts for Personal Context V2J learned graph intelligence readiness.

V2J certifies whether one specific learned graph task has enough governed data
and evaluation evidence to enter SHADOW mode.  It does not enable a model and
never grants canonical or execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LearnedGraphTask(StrEnum):
    LINK_PREDICTION = "LINK_PREDICTION"
    ENTITY_RESOLUTION = "ENTITY_RESOLUTION"
    NODE_ROLE_CLASSIFICATION = "NODE_ROLE_CLASSIFICATION"
    GRAPH_ANOMALY_SCORING = "GRAPH_ANOMALY_SCORING"
    CONTEXT_RELEVANCE_RANKING = "CONTEXT_RELEVANCE_RANKING"
    EPISODE_ASSOCIATION = "EPISODE_ASSOCIATION"
    TEMPORAL_NEXT_EVENT = "TEMPORAL_NEXT_EVENT"


class LearnedGraphRuntimeMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class LabelValue(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class DatasetLineageClass(StrEnum):
    ORGANIC = "ORGANIC"
    SYNTHETIC = "SYNTHETIC"
    HISTORICAL_UNKNOWN = "HISTORICAL_UNKNOWN"


@dataclass(frozen=True, slots=True)
class CorrectedGraphLabel:
    tenant_id: str
    task: LearnedGraphTask
    label_id: str
    group_key: str
    value: LabelValue
    decided_at: datetime
    lineage: DatasetLineageClass
    source_kind: str
    source_ref: str
    decision_kind: str
    provenance: dict[str, object]
    source_quality: str = "UNKNOWN"
    valid_from: datetime | None = None
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class DatasetLineageSummary:
    total: int
    positive: int
    negative: int
    organic: int
    organic_positive: int
    organic_negative: int
    synthetic: int
    historical_unknown: int
    corrected_groups: int
    owner_confirmed_labels: int
    owner_rejected_labels: int
    superseded_corrections: int
    inferred_unreviewed: int
    admitted_noncanonical: int
    explicit_relationships: int
    temporal_span_days: float


@dataclass(frozen=True, slots=True)
class TemporalSplitManifest:
    task: LearnedGraphTask
    train_label_ids: tuple[str, ...]
    validation_label_ids: tuple[str, ...]
    test_label_ids: tuple[str, ...]
    excluded_non_organic_label_ids: tuple[str, ...]
    train_until: datetime
    validation_until: datetime
    test_from: datetime
    fingerprint: str
    train_positive: int
    train_negative: int
    validation_positive: int
    validation_negative: int
    test_positive: int
    test_negative: int
    group_leakage_detected: bool
    lineage_leakage_detected: bool
    schema_version: str = "TEMPORAL_HOLDOUT_V0"


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    task: LearnedGraphTask
    engine_name: str
    engine_version: str
    precision: float
    recall: float
    false_positive_rate: float
    calibration_error: float
    explainability_coverage: float
    test_examples: int
    split_fingerprint: str
    reproducibility_ref: str


@dataclass(frozen=True, slots=True)
class LearnedGraphReadinessPolicy:
    minimum_total_labels: int = 200
    minimum_positive_labels: int = 50
    minimum_negative_labels: int = 50
    minimum_organic_labels: int = 150
    maximum_unknown_lineage_ratio: float = 0.05
    minimum_temporal_span_days: int = 30
    minimum_test_examples: int = 40
    minimum_precision_improvement: float = 0.02
    maximum_false_positive_rate: float = 0.05
    maximum_calibration_error: float = 0.10
    minimum_explainability_coverage: float = 0.95
    maximum_recall_regression: float = 0.02


@dataclass(frozen=True, slots=True)
class ReadinessGate:
    code: str
    passed: bool
    observed: str
    required: str


@dataclass(frozen=True, slots=True)
class LearnedGraphReadinessReport:
    tenant_id: str
    task: LearnedGraphTask
    ready_for_shadow: bool
    runtime_mode_default: LearnedGraphRuntimeMode
    dataset: DatasetLineageSummary
    split: TemporalSplitManifest | None
    baseline: EvaluationMetrics | None
    learned: EvaluationMetrics | None
    gates: tuple[ReadinessGate, ...]
    generated_at: datetime
    policy_version: str = "LEARNED_GRAPH_READINESS_V0"

    @property
    def failed_gate_codes(self) -> tuple[str, ...]:
        return tuple(gate.code for gate in self.gates if not gate.passed)


__all__ = [
    "CorrectedGraphLabel",
    "DatasetLineageClass",
    "DatasetLineageSummary",
    "EvaluationMetrics",
    "LabelValue",
    "LearnedGraphReadinessPolicy",
    "LearnedGraphReadinessReport",
    "LearnedGraphRuntimeMode",
    "LearnedGraphTask",
    "ReadinessGate",
    "TemporalSplitManifest",
]
