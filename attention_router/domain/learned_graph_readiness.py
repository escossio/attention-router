"""Contracts for Personal Context V2J learned graph readiness.

V2J is a governance/evaluation gate.  A READY result means only that one
specific learned task may enter shadow evaluation under the declared policy.
It never enables canonical writes or execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LearnedGraphTask(StrEnum):
    LINK_PREDICTION = "LINK_PREDICTION"
    NODE_ROLE_CLASSIFICATION = "NODE_ROLE_CLASSIFICATION"
    GRAPH_ANOMALY_SCORING = "GRAPH_ANOMALY_SCORING"
    CONTEXT_RELEVANCE_RANKING = "CONTEXT_RELEVANCE_RANKING"
    EPISODE_ASSOCIATION = "EPISODE_ASSOCIATION"
    TEMPORAL_NEXT_EVENT_PREDICTION = "TEMPORAL_NEXT_EVENT_PREDICTION"


class DatasetLineage(StrEnum):
    ORGANIC = "ORGANIC"
    SYNTHETIC = "SYNTHETIC"
    UNKNOWN = "UNKNOWN"


class LearnedGraphReadinessState(StrEnum):
    NOT_READY = "NOT_READY"
    READY_FOR_OFFLINE_EVALUATION = "READY_FOR_OFFLINE_EVALUATION"
    READY_FOR_SHADOW_TRIAL = "READY_FOR_SHADOW_TRIAL"


@dataclass(frozen=True, slots=True)
class DatasetLabelRecord:
    label_id: str
    label_kind: str
    label_state: str
    decided_at: datetime
    valid_from: datetime | None
    valid_until: datetime | None
    lineage: DatasetLineage
    source_quality: float | None
    tenant_id: str


@dataclass(frozen=True, slots=True)
class DatasetReadinessSnapshot:
    tenant_id: str
    dataset_fingerprint: str
    label_records: tuple[DatasetLabelRecord, ...]
    positive_label_count: int
    negative_label_count: int
    correction_label_count: int
    organic_label_count: int
    synthetic_label_count: int
    unknown_lineage_label_count: int
    unknown_source_quality_label_count: int
    mean_source_quality: float | None
    organic_mean_source_quality: float | None
    explicit_relationship_count: int
    owner_confirmed_relationship_count: int
    inferred_candidate_count: int
    earliest_decision_at: datetime | None
    latest_decision_at: datetime | None
    temporal_span_days: float
    evidence_type_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class LearnedGraphTaskPolicy:
    task: LearnedGraphTask
    justification: str
    deterministic_baseline_name: str
    deterministic_baseline_version: str
    primary_metric: str
    higher_is_better: bool
    min_positive_labels: int
    min_negative_labels: int
    min_correction_labels: int
    min_organic_labels: int
    min_temporal_span_days: int
    min_organic_source_quality: float
    max_unknown_lineage_fraction: float
    max_unknown_source_quality_fraction: float
    max_false_positive_rate: float
    max_calibration_error: float
    minimum_baseline_improvement: float = 0.0
    require_temporal_holdout: bool = True


@dataclass(frozen=True, slots=True)
class OfflineEvaluationResult:
    task: LearnedGraphTask
    tenant_id: str
    dataset_fingerprint: str
    reproducibility_ref: str
    deterministic_baseline_name: str
    deterministic_baseline_version: str
    primary_metric: str
    baseline_score: float
    learned_score: float
    false_positive_rate: float
    calibration_error: float
    train_label_ids: tuple[str, ...]
    validation_label_ids: tuple[str, ...]
    test_label_ids: tuple[str, ...]
    train_end: datetime
    validation_end: datetime
    test_start: datetime
    temporal_holdout: bool
    test_lineage_organic_only: bool
    leakage_checks_passed: bool
    explainability_path_available: bool
    shadow_mode: bool


@dataclass(frozen=True, slots=True)
class LearnedGraphReadinessReport:
    state: LearnedGraphReadinessState
    tenant_id: str
    substrate_version: str
    dataset: DatasetReadinessSnapshot
    task: LearnedGraphTask | None
    feature_flag_enabled: bool
    shadow_mode_configured: bool
    reasons: tuple[str, ...]
    evaluation_fingerprint: str | None = None
    generated_at: datetime | None = None

    @property
    def ready_for_learned_runtime(self) -> bool:
        """V2J never grants production learned-runtime permission."""
        return False


__all__ = [
    "DatasetLabelRecord",
    "DatasetLineage",
    "DatasetReadinessSnapshot",
    "LearnedGraphReadinessReport",
    "LearnedGraphReadinessState",
    "LearnedGraphTask",
    "LearnedGraphTaskPolicy",
    "OfflineEvaluationResult",
]
