"""Learned Graph Intelligence readiness gates for Personal Context V2J.

This module is read-only.  It inventories governed corrected labels, builds a
reproducible organic-only temporal holdout, and evaluates offline metrics
against a deterministic baseline.  It never enables a learned model.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.domain.learned_graph_readiness import (
    CorrectedGraphLabel,
    DatasetLineageClass,
    DatasetLineageSummary,
    EvaluationMetrics,
    LabelValue,
    LearnedGraphReadinessPolicy,
    LearnedGraphReadinessReport,
    LearnedGraphRuntimeMode,
    LearnedGraphTask,
    ReadinessGate,
    TemporalSplitManifest,
)
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
    CandidateInsightRow,
)
from attention_router.infrastructure.entity_resolution_models import (
    EntityResolutionCandidateRow,
    EntityResolutionEvidenceRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    RelationshipRow,
    TenantRow,
    TimelineEventRow,
)


DEFAULT_LEARNED_GRAPH_RUNTIME_MODE: Final = LearnedGraphRuntimeMode.OFF
SUPPORTED_READINESS_TASKS: Final = {
    LearnedGraphTask.LINK_PREDICTION,
    LearnedGraphTask.ENTITY_RESOLUTION,
}
EXPECTED_BASELINE_ENGINE: Final = {
    LearnedGraphTask.LINK_PREDICTION: "DETERMINISTIC_RELATION_CANDIDATE",
    LearnedGraphTask.ENTITY_RESOLUTION: "DETERMINISTIC_ENTITY_RESOLUTION_V0",
}
_RECOGNIZED_LINEAGE: Final = {
    "ORGANIC": DatasetLineageClass.ORGANIC,
    "SYNTHETIC": DatasetLineageClass.SYNTHETIC,
    "HISTORICAL_UNKNOWN": DatasetLineageClass.HISTORICAL_UNKNOWN,
    "UNKNOWN": DatasetLineageClass.HISTORICAL_UNKNOWN,
}


class LearnedGraphReadinessError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lineage(value: object) -> DatasetLineageClass:
    normalized = str(value or "").strip().upper()
    return _RECOGNIZED_LINEAGE.get(
        normalized,
        DatasetLineageClass.HISTORICAL_UNKNOWN,
    )


def _combine_lineages(
    lineages: list[DatasetLineageClass],
) -> DatasetLineageClass:
    if not lineages:
        return DatasetLineageClass.HISTORICAL_UNKNOWN
    if DatasetLineageClass.SYNTHETIC in lineages:
        return DatasetLineageClass.SYNTHETIC
    if all(item == DatasetLineageClass.ORGANIC for item in lineages):
        return DatasetLineageClass.ORGANIC
    return DatasetLineageClass.HISTORICAL_UNKNOWN


def _timeline_event_lineage(
    session: Session,
    *,
    tenant_id: str,
    event_id: str,
) -> DatasetLineageClass:
    event = session.get(TimelineEventRow, event_id)
    if event is None or event.tenant_id != tenant_id:
        return DatasetLineageClass.HISTORICAL_UNKNOWN

    if event.canonical_event_id:
        canonical = session.get(
            CanonicalEventRow,
            event.canonical_event_id,
        )
        if canonical is not None and canonical.tenant_id == tenant_id:
            return _lineage(canonical.lineage_classification)

    metadata = event.metadata_json or {}
    return _lineage(metadata.get("lineage_classification"))


def _candidate_insight_profile(
    session: Session,
    *,
    candidate: CandidateInsightRow,
) -> tuple[DatasetLineageClass, str]:
    evidence_rows = list(
        session.scalars(
            select(CandidateInsightEvidenceRow)
            .where(
                CandidateInsightEvidenceRow.tenant_id
                == candidate.tenant_id,
                CandidateInsightEvidenceRow.candidate_id
                == candidate.id,
            )
            .order_by(
                CandidateInsightEvidenceRow.evidence_type,
                CandidateInsightEvidenceRow.source_ref,
            )
        ).all()
    )
    lineages: list[DatasetLineageClass] = []
    for evidence in evidence_rows:
        if evidence.evidence_type == "TIMELINE_EVENT":
            lineages.append(
                _timeline_event_lineage(
                    session,
                    tenant_id=candidate.tenant_id,
                    event_id=evidence.source_ref,
                )
            )
            continue
        explicit = (evidence.provenance or {}).get(
            "lineage_classification"
        )
        lineages.append(_lineage(explicit))
    evidence_types = sorted(
        {row.evidence_type for row in evidence_rows}
    )
    quality = (
        f"ENGINE:{candidate.source_engine}|EVIDENCE:"
        + ",".join(evidence_types)
        if evidence_types
        else f"ENGINE:{candidate.source_engine}|EVIDENCE:UNKNOWN"
    )
    return _combine_lineages(lineages), quality


def _entity_resolution_profile(
    session: Session,
    *,
    candidate: EntityResolutionCandidateRow,
) -> tuple[DatasetLineageClass, str]:
    rows = list(
        session.scalars(
            select(EntityResolutionEvidenceRow)
            .where(
                EntityResolutionEvidenceRow.candidate_id
                == candidate.id
            )
            .order_by(
                EntityResolutionEvidenceRow.evidence_type,
                EntityResolutionEvidenceRow.source_ref,
            )
        ).all()
    )
    lineage = _combine_lineages(
        [
            _lineage(
                (row.metadata_json or {}).get(
                    "lineage_classification"
                )
            )
            for row in rows
        ]
    )
    evidence_types = sorted({row.evidence_type for row in rows})
    quality = (
        "EVIDENCE:" + ",".join(evidence_types)
        if evidence_types
        else "EVIDENCE:UNKNOWN"
    )
    return lineage, quality


def _link_prediction_labels(
    session: Session,
    *,
    tenant_id: str,
) -> tuple[list[CorrectedGraphLabel], int, int, int]:
    rows = list(
        session.scalars(
            select(CandidateInsightRow)
            .where(
                CandidateInsightRow.tenant_id == tenant_id,
                CandidateInsightRow.insight_type
                == "RELATIONSHIP_PROPOSAL",
            )
            .order_by(
                CandidateInsightRow.updated_at,
                CandidateInsightRow.id,
            )
        ).all()
    )
    labels: list[CorrectedGraphLabel] = []
    superseded = 0
    inferred_unreviewed = 0
    admitted_noncanonical = 0

    for row in rows:
        if row.state == "SUPERSEDED":
            superseded += 1
            continue
        if row.state in {"PROPOSED", "NEEDS_REVIEW"}:
            inferred_unreviewed += 1
            continue
        if (
            row.state == "ADMITTED"
            and row.decision_kind == "OWNER_ADMITTED"
        ):
            admitted_noncanonical += 1
            continue
        if row.sensitivity_class == "SECRET":
            continue
        if row.decided_at is None:
            continue

        if (
            row.state == "REJECTED"
            and row.decision_kind == "OWNER_REJECTED"
        ):
            value = LabelValue.NEGATIVE
        else:
            continue

        lineage, source_quality = _candidate_insight_profile(
            session,
            candidate=row,
        )
        labels.append(
            CorrectedGraphLabel(
                tenant_id=tenant_id,
                task=LearnedGraphTask.LINK_PREDICTION,
                label_id=row.id,
                group_key=row.semantic_key,
                value=value,
                decided_at=_utc(row.decided_at),
                lineage=lineage,
                source_kind="CANDIDATE_INSIGHT",
                source_ref=row.id,
                decision_kind=row.decision_kind,
                provenance={
                    "source_engine": row.source_engine,
                    "predicate": row.predicate,
                    "sensitivity_class": row.sensitivity_class,
                    "supersedes_insight_id": row.supersedes_insight_id,
                },
                source_quality=source_quality,
                valid_from=(
                    _utc(row.valid_from)
                    if row.valid_from is not None
                    else None
                ),
                valid_until=(
                    _utc(row.valid_until)
                    if row.valid_until is not None
                    else None
                ),
            )
        )

    return (
        labels,
        superseded,
        inferred_unreviewed,
        admitted_noncanonical,
    )


def _entity_resolution_labels(
    session: Session,
    *,
    tenant_id: str,
) -> tuple[list[CorrectedGraphLabel], int, int, int]:
    rows = list(
        session.scalars(
            select(EntityResolutionCandidateRow)
            .where(
                EntityResolutionCandidateRow.tenant_id == tenant_id
            )
            .order_by(
                EntityResolutionCandidateRow.updated_at,
                EntityResolutionCandidateRow.id,
            )
        ).all()
    )
    labels: list[CorrectedGraphLabel] = []
    superseded = 0
    inferred_unreviewed = 0

    for row in rows:
        if row.state == "SUPERSEDED":
            superseded += 1
            continue
        if row.state in {"PROPOSED", "AMBIGUOUS"}:
            inferred_unreviewed += 1
            continue
        if row.decided_at is None:
            continue

        if (
            row.state == "CONFIRMED"
            and row.decision_kind == "OWNER_CONFIRMED"
        ):
            value = LabelValue.POSITIVE
        elif (
            row.state == "REJECTED"
            and row.decision_kind == "OWNER_REJECTED"
        ):
            value = LabelValue.NEGATIVE
        else:
            continue

        pair_key = stable_hash(
            {
                "left_actor_key": row.left_actor_key,
                "right_actor_key": row.right_actor_key,
            }
        )
        lineage, source_quality = _entity_resolution_profile(
            session,
            candidate=row,
        )
        labels.append(
            CorrectedGraphLabel(
                tenant_id=tenant_id,
                task=LearnedGraphTask.ENTITY_RESOLUTION,
                label_id=row.id,
                group_key=f"entity-pair:{pair_key[:96]}",
                value=value,
                decided_at=_utc(row.decided_at),
                lineage=lineage,
                source_kind="ENTITY_RESOLUTION_CANDIDATE",
                source_ref=row.id,
                decision_kind=row.decision_kind,
                provenance={
                    "confidence": row.confidence,
                    "evidence_count": row.evidence_count,
                    "canonical_actor_key": row.canonical_actor_key,
                },
                source_quality=source_quality,
                valid_from=None,
                valid_until=None,
            )
        )

    return labels, superseded, inferred_unreviewed, 0


def collect_corrected_graph_labels(
    session: Session,
    *,
    tenant_id: str,
    task: LearnedGraphTask,
) -> tuple[tuple[CorrectedGraphLabel, ...], int, int, int]:
    """Collect only owner-decided labels for one task."""
    if session.get(TenantRow, tenant_id) is None:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_TENANT_NOT_FOUND"
        )
    if task == LearnedGraphTask.LINK_PREDICTION:
        labels, superseded, pending, admitted = _link_prediction_labels(
            session,
            tenant_id=tenant_id,
        )
    elif task == LearnedGraphTask.ENTITY_RESOLUTION:
        labels, superseded, pending, admitted = _entity_resolution_labels(
            session,
            tenant_id=tenant_id,
        )
    else:
        return (), 0, 0, 0

    labels.sort(key=lambda item: (item.decided_at, item.label_id))
    return tuple(labels), superseded, pending, admitted


def summarize_dataset_lineage(
    session: Session,
    *,
    tenant_id: str,
    labels: tuple[CorrectedGraphLabel, ...],
    superseded_corrections: int,
    inferred_unreviewed: int,
    admitted_noncanonical: int,
) -> DatasetLineageSummary:
    counter = Counter(item.lineage for item in labels)
    positive = sum(
        item.value == LabelValue.POSITIVE for item in labels
    )
    negative = sum(
        item.value == LabelValue.NEGATIVE for item in labels
    )
    organic_labels = [
        item
        for item in labels
        if item.lineage == DatasetLineageClass.ORGANIC
    ]
    organic_positive = sum(
        item.value == LabelValue.POSITIVE
        for item in organic_labels
    )
    organic_negative = sum(
        item.value == LabelValue.NEGATIVE
        for item in organic_labels
    )
    organic_times = sorted(
        item.decided_at
        for item in organic_labels
    )
    temporal_span_days = 0.0
    if len(organic_times) >= 2:
        temporal_span_days = (
            organic_times[-1] - organic_times[0]
        ).total_seconds() / 86400.0

    explicit_relationships = int(
        session.scalar(
            select(func.count())
            .select_from(RelationshipRow)
            .where(RelationshipRow.tenant_id == tenant_id)
        )
        or 0
    )
    return DatasetLineageSummary(
        total=len(labels),
        positive=positive,
        negative=negative,
        organic=counter[DatasetLineageClass.ORGANIC],
        organic_positive=organic_positive,
        organic_negative=organic_negative,
        synthetic=counter[DatasetLineageClass.SYNTHETIC],
        historical_unknown=counter[
            DatasetLineageClass.HISTORICAL_UNKNOWN
        ],
        corrected_groups=len({item.group_key for item in labels}),
        owner_confirmed_labels=positive,
        owner_rejected_labels=negative,
        superseded_corrections=superseded_corrections,
        inferred_unreviewed=inferred_unreviewed,
        admitted_noncanonical=admitted_noncanonical,
        explicit_relationships=explicit_relationships,
        temporal_span_days=round(temporal_span_days, 6),
    )


def build_temporal_holdout(
    labels: tuple[CorrectedGraphLabel, ...],
    *,
    task: LearnedGraphTask,
) -> TemporalSplitManifest | None:
    """Build deterministic 70/15/15 organic-only temporal holdout."""
    organic = sorted(
        (
            item
            for item in labels
            if item.lineage == DatasetLineageClass.ORGANIC
        ),
        key=lambda item: (item.decided_at, item.label_id),
    )
    excluded = tuple(
        sorted(
            item.label_id
            for item in labels
            if item.lineage != DatasetLineageClass.ORGANIC
        )
    )
    if len(organic) < 3:
        return None

    total = len(organic)
    train_count = max(1, int(total * 0.70))
    validation_count = max(1, int(total * 0.15))
    if train_count + validation_count >= total:
        validation_count = 1
        train_count = total - 2

    train = organic[:train_count]
    validation = organic[
        train_count : train_count + validation_count
    ]
    test = organic[train_count + validation_count :]
    if not train or not validation or not test:
        return None

    groups = {
        "train": {item.group_key for item in train},
        "validation": {item.group_key for item in validation},
        "test": {item.group_key for item in test},
    }
    group_leakage = bool(
        (groups["train"] & groups["validation"])
        or (groups["train"] & groups["test"])
        or (groups["validation"] & groups["test"])
    )
    lineage_leakage = any(
        item.lineage != DatasetLineageClass.ORGANIC
        for item in [*validation, *test]
    )

    train_ids = tuple(item.label_id for item in train)
    validation_ids = tuple(item.label_id for item in validation)
    test_ids = tuple(item.label_id for item in test)
    train_positive = sum(
        item.value == LabelValue.POSITIVE for item in train
    )
    train_negative = sum(
        item.value == LabelValue.NEGATIVE for item in train
    )
    validation_positive = sum(
        item.value == LabelValue.POSITIVE for item in validation
    )
    validation_negative = sum(
        item.value == LabelValue.NEGATIVE for item in validation
    )
    test_positive = sum(
        item.value == LabelValue.POSITIVE for item in test
    )
    test_negative = sum(
        item.value == LabelValue.NEGATIVE for item in test
    )
    train_until = train[-1].decided_at
    validation_until = validation[-1].decided_at
    test_from = test[0].decided_at
    fingerprint = stable_hash(
        {
            "schema_version": "TEMPORAL_HOLDOUT_V0",
            "task": task.value,
            "train_label_ids": train_ids,
            "validation_label_ids": validation_ids,
            "test_label_ids": test_ids,
            "excluded_non_organic_label_ids": excluded,
            "train_until": train_until.isoformat(),
            "validation_until": validation_until.isoformat(),
            "test_from": test_from.isoformat(),
            "train_positive": train_positive,
            "train_negative": train_negative,
            "validation_positive": validation_positive,
            "validation_negative": validation_negative,
            "test_positive": test_positive,
            "test_negative": test_negative,
        }
    )

    return TemporalSplitManifest(
        task=task,
        train_label_ids=train_ids,
        validation_label_ids=validation_ids,
        test_label_ids=test_ids,
        excluded_non_organic_label_ids=excluded,
        train_until=train_until,
        validation_until=validation_until,
        test_from=test_from,
        fingerprint=fingerprint,
        train_positive=train_positive,
        train_negative=train_negative,
        validation_positive=validation_positive,
        validation_negative=validation_negative,
        test_positive=test_positive,
        test_negative=test_negative,
        group_leakage_detected=group_leakage,
        lineage_leakage_detected=lineage_leakage,
    )


def _validate_policy(
    policy: LearnedGraphReadinessPolicy,
) -> None:
    integer_values = {
        "minimum_total_labels": policy.minimum_total_labels,
        "minimum_positive_labels": policy.minimum_positive_labels,
        "minimum_negative_labels": policy.minimum_negative_labels,
        "minimum_organic_labels": policy.minimum_organic_labels,
        "minimum_temporal_span_days": policy.minimum_temporal_span_days,
        "minimum_test_examples": policy.minimum_test_examples,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in integer_values.values()
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_POLICY_INTEGER_INVALID"
        )
    if (
        policy.minimum_total_labels
        < policy.minimum_positive_labels
        + policy.minimum_negative_labels
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_POLICY_LABEL_COUNTS_INVALID"
        )
    if policy.minimum_organic_labels > policy.minimum_total_labels:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_POLICY_ORGANIC_INVALID"
        )
    if (
        policy.minimum_organic_labels
        < policy.minimum_positive_labels
        + policy.minimum_negative_labels
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_POLICY_ORGANIC_CLASS_COUNTS_INVALID"
        )

    ratio_values = (
        policy.maximum_unknown_lineage_ratio,
        policy.minimum_precision_improvement,
        policy.maximum_false_positive_rate,
        policy.maximum_calibration_error,
        policy.minimum_explainability_coverage,
        policy.maximum_recall_regression,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or value > 1
        for value in ratio_values
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_POLICY_RATIO_INVALID"
        )


def _metric_valid(value: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and 0.0 <= float(value) <= 1.0
    )


def _validate_metrics(
    metrics: EvaluationMetrics | None,
) -> bool:
    if metrics is None:
        return False
    return (
        all(
            _metric_valid(value)
            for value in (
                metrics.precision,
                metrics.recall,
                metrics.false_positive_rate,
                metrics.calibration_error,
                metrics.explainability_coverage,
            )
        )
        and metrics.test_examples >= 0
        and bool(metrics.engine_name.strip())
        and bool(metrics.engine_version.strip())
        and bool(metrics.split_fingerprint.strip())
        and bool(metrics.reproducibility_ref.strip())
    )


def _gate(
    code: str,
    passed: bool,
    observed: object,
    required: object,
) -> ReadinessGate:
    return ReadinessGate(
        code=code,
        passed=bool(passed),
        observed=str(observed),
        required=str(required),
    )


def evaluate_learned_graph_readiness(
    session: Session,
    *,
    tenant_id: str,
    task: LearnedGraphTask,
    baseline: EvaluationMetrics | None = None,
    learned: EvaluationMetrics | None = None,
    policy: LearnedGraphReadinessPolicy | None = None,
    now: datetime | None = None,
) -> LearnedGraphReadinessReport:
    """Evaluate readiness for SHADOW mode; default runtime remains OFF."""
    current_policy = policy or LearnedGraphReadinessPolicy()
    _validate_policy(current_policy)
    labels, superseded, pending, admitted = collect_corrected_graph_labels(
        session,
        tenant_id=tenant_id,
        task=task,
    )
    dataset = summarize_dataset_lineage(
        session,
        tenant_id=tenant_id,
        labels=labels,
        superseded_corrections=superseded,
        inferred_unreviewed=pending,
        admitted_noncanonical=admitted,
    )
    split = build_temporal_holdout(labels, task=task)

    total = max(1, dataset.total)
    unknown_ratio = dataset.historical_unknown / total
    baseline_valid = _validate_metrics(baseline)
    learned_valid = _validate_metrics(learned)
    supported = task in SUPPORTED_READINESS_TASKS
    expected_baseline = EXPECTED_BASELINE_ENGINE.get(task)

    gates: list[ReadinessGate] = [
        _gate(
            "TASK_LABEL_ADAPTER_SUPPORTED",
            supported,
            task.value,
            "V2J_SUPPORTED_TASK",
        ),
        _gate(
            "MINIMUM_TOTAL_LABELS",
            dataset.total >= current_policy.minimum_total_labels,
            dataset.total,
            current_policy.minimum_total_labels,
        ),
        _gate(
            "MINIMUM_ORGANIC_POSITIVE_LABELS",
            dataset.organic_positive
            >= current_policy.minimum_positive_labels,
            dataset.organic_positive,
            current_policy.minimum_positive_labels,
        ),
        _gate(
            "MINIMUM_ORGANIC_NEGATIVE_LABELS",
            dataset.organic_negative
            >= current_policy.minimum_negative_labels,
            dataset.organic_negative,
            current_policy.minimum_negative_labels,
        ),
        _gate(
            "MINIMUM_ORGANIC_LABELS",
            dataset.organic
            >= current_policy.minimum_organic_labels,
            dataset.organic,
            current_policy.minimum_organic_labels,
        ),
        _gate(
            "UNKNOWN_LINEAGE_RATIO",
            dataset.total > 0
            and unknown_ratio
            <= current_policy.maximum_unknown_lineage_ratio,
            round(unknown_ratio, 6),
            f"<={current_policy.maximum_unknown_lineage_ratio}",
        ),
        _gate(
            "MINIMUM_TEMPORAL_SPAN",
            dataset.temporal_span_days
            >= current_policy.minimum_temporal_span_days,
            dataset.temporal_span_days,
            current_policy.minimum_temporal_span_days,
        ),
        _gate(
            "TEMPORAL_HOLDOUT_PRESENT",
            split is not None,
            bool(split),
            True,
        ),
        _gate(
            "TEMPORAL_ORDER_STRICT",
            split is not None
            and split.train_until < split.validation_until
            and split.validation_until < split.test_from,
            (
                (
                    split.train_until.isoformat(),
                    split.validation_until.isoformat(),
                    split.test_from.isoformat(),
                )
                if split is not None
                else "NO_SPLIT"
            ),
            "train_until < validation_until < test_from",
        ),
        _gate(
            "GROUP_LEAKAGE_PREVENTED",
            split is not None
            and not split.group_leakage_detected,
            (
                split.group_leakage_detected
                if split is not None
                else "NO_SPLIT"
            ),
            False,
        ),
        _gate(
            "LINEAGE_LEAKAGE_PREVENTED",
            split is not None
            and not split.lineage_leakage_detected,
            (
                split.lineage_leakage_detected
                if split is not None
                else "NO_SPLIT"
            ),
            False,
        ),
        _gate(
            "TRAIN_CLASS_BALANCE",
            split is not None
            and split.train_positive > 0
            and split.train_negative > 0,
            (
                (split.train_positive, split.train_negative)
                if split is not None
                else "NO_SPLIT"
            ),
            "positive>0 and negative>0",
        ),
        _gate(
            "VALIDATION_CLASS_BALANCE",
            split is not None
            and split.validation_positive > 0
            and split.validation_negative > 0,
            (
                (split.validation_positive, split.validation_negative)
                if split is not None
                else "NO_SPLIT"
            ),
            "positive>0 and negative>0",
        ),
        _gate(
            "TEST_CLASS_BALANCE",
            split is not None
            and split.test_positive > 0
            and split.test_negative > 0,
            (
                (split.test_positive, split.test_negative)
                if split is not None
                else "NO_SPLIT"
            ),
            "positive>0 and negative>0",
        ),
        _gate(
            "BASELINE_METRICS_PRESENT",
            baseline_valid,
            baseline.engine_name if baseline else "MISSING",
            "VALID_BASELINE_METRICS",
        ),
        _gate(
            "DETERMINISTIC_BASELINE_MATCHES_TASK",
            baseline_valid
            and baseline is not None
            and baseline.task == task
            and baseline.engine_name == expected_baseline,
            (
                baseline.engine_name
                if baseline is not None
                else "MISSING"
            ),
            expected_baseline or "UNSUPPORTED",
        ),
        _gate(
            "LEARNED_METRICS_PRESENT",
            learned_valid,
            learned.engine_name if learned else "MISSING",
            "VALID_LEARNED_METRICS",
        ),
    ]

    split_test_examples = (
        len(split.test_label_ids) if split is not None else 0
    )
    metrics_match_split = bool(
        baseline_valid
        and learned_valid
        and baseline is not None
        and learned is not None
        and baseline.task == task
        and learned.task == task
        and baseline.test_examples == split_test_examples
        and learned.test_examples == split_test_examples
        and split is not None
        and baseline.split_fingerprint == split.fingerprint
        and learned.split_fingerprint == split.fingerprint
    )
    gates.append(
        _gate(
            "EVALUATION_MATCHES_TEMPORAL_HOLDOUT",
            metrics_match_split,
            (
                f"baseline_examples={baseline.test_examples},"
                f"learned_examples={learned.test_examples},"
                f"split_examples={split_test_examples},"
                f"baseline_split={baseline.split_fingerprint},"
                f"learned_split={learned.split_fingerprint},"
                f"expected_split={split.fingerprint if split else 'NO_SPLIT'}"
                if baseline is not None and learned is not None
                else "MISSING_METRICS"
            ),
            (
                f"examples={split_test_examples},"
                f"split={split.fingerprint if split else 'NO_SPLIT'}"
            ),
        )
    )
    gates.append(
        _gate(
            "MINIMUM_TEST_EXAMPLES",
            split_test_examples
            >= current_policy.minimum_test_examples,
            split_test_examples,
            current_policy.minimum_test_examples,
        )
    )

    if baseline_valid and learned_valid and baseline and learned:
        gates.extend(
            [
                _gate(
                    "LEARNED_BEATS_BASELINE_PRECISION",
                    learned.precision
                    >= baseline.precision
                    + current_policy.minimum_precision_improvement,
                    round(
                        learned.precision - baseline.precision,
                        6,
                    ),
                    f">={current_policy.minimum_precision_improvement}",
                ),
                _gate(
                    "RECALL_REGRESSION_WITHIN_BUDGET",
                    learned.recall
                    >= baseline.recall
                    - current_policy.maximum_recall_regression,
                    round(
                        learned.recall - baseline.recall,
                        6,
                    ),
                    f">=-{current_policy.maximum_recall_regression}",
                ),
                _gate(
                    "FALSE_POSITIVE_BUDGET",
                    learned.false_positive_rate
                    <= current_policy.maximum_false_positive_rate,
                    learned.false_positive_rate,
                    f"<={current_policy.maximum_false_positive_rate}",
                ),
                _gate(
                    "CALIBRATION_BUDGET",
                    learned.calibration_error
                    <= current_policy.maximum_calibration_error,
                    learned.calibration_error,
                    f"<={current_policy.maximum_calibration_error}",
                ),
                _gate(
                    "EXPLAINABILITY_COVERAGE",
                    learned.explainability_coverage
                    >= current_policy.minimum_explainability_coverage,
                    learned.explainability_coverage,
                    f">={current_policy.minimum_explainability_coverage}",
                ),
                _gate(
                    "LEARNED_ENGINE_DIFFERS_FROM_BASELINE",
                    learned.engine_name != baseline.engine_name,
                    learned.engine_name,
                    "different-from-baseline",
                ),
            ]
        )
    else:
        for code in (
            "LEARNED_BEATS_BASELINE_PRECISION",
            "RECALL_REGRESSION_WITHIN_BUDGET",
            "FALSE_POSITIVE_BUDGET",
            "CALIBRATION_BUDGET",
            "EXPLAINABILITY_COVERAGE",
            "LEARNED_ENGINE_DIFFERS_FROM_BASELINE",
        ):
            gates.append(
                _gate(
                    code,
                    False,
                    "MISSING_VALID_METRICS",
                    "VALID_BASELINE_AND_LEARNED_METRICS",
                )
            )

    gates.append(
        _gate(
            "RUNTIME_DEFAULT_OFF",
            DEFAULT_LEARNED_GRAPH_RUNTIME_MODE
            == LearnedGraphRuntimeMode.OFF,
            DEFAULT_LEARNED_GRAPH_RUNTIME_MODE.value,
            "OFF",
        )
    )

    ready = all(gate.passed for gate in gates)
    stamp = _utc(now or datetime.now(UTC))
    return LearnedGraphReadinessReport(
        tenant_id=tenant_id,
        task=task,
        ready_for_shadow=ready,
        runtime_mode_default=DEFAULT_LEARNED_GRAPH_RUNTIME_MODE,
        dataset=dataset,
        split=split,
        baseline=baseline,
        learned=learned,
        gates=tuple(gates),
        generated_at=stamp,
    )


def authorize_learned_graph_runtime_mode(
    report: LearnedGraphReadinessReport,
    *,
    requested_mode: LearnedGraphRuntimeMode,
    feature_flag_enabled: bool = False,
) -> LearnedGraphRuntimeMode:
    """V2J permits OFF or readiness-gated SHADOW only. ACTIVE is forbidden."""
    if requested_mode == LearnedGraphRuntimeMode.OFF:
        return LearnedGraphRuntimeMode.OFF
    if requested_mode == LearnedGraphRuntimeMode.ACTIVE:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_ACTIVE_MODE_NOT_ALLOWED_V2J"
        )
    if not feature_flag_enabled:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_SHADOW_FEATURE_FLAG_REQUIRED"
        )
    if not report.ready_for_shadow:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_SHADOW_READINESS_REQUIRED"
        )
    return LearnedGraphRuntimeMode.SHADOW


__all__ = [
    "DEFAULT_LEARNED_GRAPH_RUNTIME_MODE",
    "EXPECTED_BASELINE_ENGINE",
    "LearnedGraphReadinessError",
    "SUPPORTED_READINESS_TASKS",
    "authorize_learned_graph_runtime_mode",
    "build_temporal_holdout",
    "collect_corrected_graph_labels",
    "evaluate_learned_graph_readiness",
    "summarize_dataset_lineage",
]
