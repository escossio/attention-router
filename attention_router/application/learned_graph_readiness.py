"""Personal Context V2J learned graph readiness gate.

The gate audits existing owner-corrected graph labels and validates one
task-specific offline evaluation manifest.  It never enables a learned model,
never changes canonical truth, and never grants execution authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.config import Settings
from attention_router.domain.learned_graph_readiness import (
    DatasetLabelRecord,
    DatasetLineage,
    DatasetReadinessSnapshot,
    LearnedGraphReadinessReport,
    LearnedGraphReadinessState,
    LearnedGraphTaskPolicy,
    OfflineEvaluationResult,
)
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
    CandidateInsightRow,
)
from attention_router.infrastructure.entity_resolution_models import (
    EntityAliasResolutionRow,
    EntityResolutionCandidateRow,
    EntityResolutionEvidenceRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    ConversationMessageRow,
    RelationshipRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.semantic_episode_models import (
    SemanticEpisodeMembershipRow,
    SemanticEpisodeRow,
)


SUBSTRATE_VERSION = "V2I"


class LearnedGraphReadinessError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lineage_from_value(value: Any) -> DatasetLineage:
    if value is None:
        return DatasetLineage.UNKNOWN
    normalized = str(value).strip().upper()
    if normalized in {"ORGANIC", "PRODUCTION"}:
        return DatasetLineage.ORGANIC
    if normalized in {
        "SYNTHETIC",
        "TEST",
        "TEST_ONLY",
        "FIXTURE",
        "UNIT_TEST",
    }:
        return DatasetLineage.SYNTHETIC
    if "SYNTHETIC" in normalized or "TEST" in normalized:
        return DatasetLineage.SYNTHETIC
    if normalized in {"UNKNOWN", "HISTORICAL_UNKNOWN", ""}:
        return DatasetLineage.UNKNOWN
    return DatasetLineage.UNKNOWN


def _source_quality_from_metadata(
    metadata: dict | None,
) -> float | None:
    payload = metadata or {}
    raw = payload.get(
        "source_quality",
        payload.get("source_quality_score"),
    )
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or raw < 0
        or raw > 1
    ):
        return None
    return round(float(raw), 6)


def _lineage_from_metadata(metadata: dict | None) -> DatasetLineage:
    payload = metadata or {}
    for key in (
        "lineage_classification",
        "environment_classification",
        "lineage_class",
    ):
        if key in payload:
            return _lineage_from_value(payload.get(key))
    return DatasetLineage.UNKNOWN


def _combine_lineages(values: list[DatasetLineage]) -> DatasetLineage:
    if not values:
        return DatasetLineage.UNKNOWN
    if any(value is DatasetLineage.SYNTHETIC for value in values):
        return DatasetLineage.SYNTHETIC
    if any(value is DatasetLineage.UNKNOWN for value in values):
        return DatasetLineage.UNKNOWN
    return DatasetLineage.ORGANIC


def _timeline_lineage(
    session: Session,
    *,
    tenant_id: str,
    source_ref: str,
) -> DatasetLineage:
    event = session.get(TimelineEventRow, source_ref)
    if event is None or event.tenant_id != tenant_id:
        return DatasetLineage.UNKNOWN
    metadata_lineage = _lineage_from_metadata(event.metadata_json)
    if metadata_lineage is not DatasetLineage.UNKNOWN:
        return metadata_lineage
    if event.canonical_event_id:
        canonical = session.get(CanonicalEventRow, event.canonical_event_id)
        if canonical is not None and canonical.tenant_id == tenant_id:
            return _lineage_from_value(canonical.lineage_classification)
    provenance = (event.provenance or "").strip()
    if not provenance:
        return DatasetLineage.UNKNOWN
    normalized = provenance.upper()
    if normalized in {"UNKNOWN", "HISTORICAL_UNKNOWN"}:
        return DatasetLineage.UNKNOWN
    if "TEST" in normalized or "SYNTHETIC" in normalized:
        return DatasetLineage.SYNTHETIC
    return DatasetLineage.ORGANIC


def _message_lineage(
    session: Session,
    *,
    tenant_id: str,
    source_ref: str,
) -> DatasetLineage:
    message = session.get(ConversationMessageRow, source_ref)
    if message is None or message.tenant_id != tenant_id:
        return DatasetLineage.UNKNOWN
    metadata_lineage = _lineage_from_metadata(message.metadata_json)
    if metadata_lineage is not DatasetLineage.UNKNOWN:
        return metadata_lineage
    source = (message.source or "").strip()
    if not source:
        return DatasetLineage.UNKNOWN
    normalized = source.upper()
    if normalized in {"UNKNOWN", "HISTORICAL_UNKNOWN"}:
        return DatasetLineage.UNKNOWN
    if "TEST" in normalized or "SYNTHETIC" in normalized:
        return DatasetLineage.SYNTHETIC
    return DatasetLineage.ORGANIC


def _episode_lineage(
    session: Session,
    *,
    tenant_id: str,
    source_ref: str,
) -> DatasetLineage:
    episode = session.get(SemanticEpisodeRow, source_ref)
    if episode is None or episode.tenant_id != tenant_id:
        return DatasetLineage.UNKNOWN

    explicit = _lineage_from_metadata(episode.provenance)
    if explicit is not DatasetLineage.UNKNOWN:
        return explicit

    memberships = list(
        session.scalars(
            select(SemanticEpisodeMembershipRow).where(
                SemanticEpisodeMembershipRow.tenant_id == tenant_id,
                SemanticEpisodeMembershipRow.episode_id == episode.id,
                SemanticEpisodeMembershipRow.ambiguous.is_(False),
            )
        ).all()
    )
    lineages: list[DatasetLineage] = []
    for membership in memberships:
        if membership.member_type == "TIMELINE_EVENT":
            lineages.append(
                _timeline_lineage(
                    session,
                    tenant_id=tenant_id,
                    source_ref=membership.member_ref,
                )
            )
        elif membership.member_type == "CONVERSATION_MESSAGE":
            lineages.append(
                _message_lineage(
                    session,
                    tenant_id=tenant_id,
                    source_ref=membership.member_ref,
                )
            )
        else:
            lineages.append(DatasetLineage.UNKNOWN)
    return _combine_lineages(lineages)


def _candidate_evidence_lineage(
    session: Session,
    *,
    tenant_id: str,
    candidate_id: str,
) -> tuple[DatasetLineage, float | None, dict[str, int]]:
    rows = list(
        session.scalars(
            select(CandidateInsightEvidenceRow).where(
                CandidateInsightEvidenceRow.tenant_id == tenant_id,
                CandidateInsightEvidenceRow.candidate_id == candidate_id,
            )
        ).all()
    )
    lineages: list[DatasetLineage] = []
    source_qualities: list[float | None] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.evidence_type] = counts.get(row.evidence_type, 0) + 1
        source_qualities.append(
            _source_quality_from_metadata(row.provenance)
        )
        explicit = _lineage_from_metadata(row.provenance)
        if explicit is not DatasetLineage.UNKNOWN:
            lineages.append(explicit)
            continue
        if row.evidence_type == "TIMELINE_EVENT":
            lineages.append(
                _timeline_lineage(
                    session,
                    tenant_id=tenant_id,
                    source_ref=row.source_ref,
                )
            )
        elif row.evidence_type == "CONVERSATION_MESSAGE":
            lineages.append(
                _message_lineage(
                    session,
                    tenant_id=tenant_id,
                    source_ref=row.source_ref,
                )
            )
        elif row.evidence_type == "SEMANTIC_EPISODE":
            lineages.append(
                _episode_lineage(
                    session,
                    tenant_id=tenant_id,
                    source_ref=row.source_ref,
                )
            )
        else:
            lineages.append(DatasetLineage.UNKNOWN)
    quality = (
        round(
            sum(value for value in source_qualities if value is not None)
            / len(source_qualities),
            6,
        )
        if source_qualities
        and all(value is not None for value in source_qualities)
        else None
    )
    return _combine_lineages(lineages), quality, counts


def _entity_resolution_lineage(
    session: Session,
    *,
    candidate_id: str,
) -> tuple[DatasetLineage, float, dict[str, int]]:
    rows = list(
        session.scalars(
            select(EntityResolutionEvidenceRow).where(
                EntityResolutionEvidenceRow.candidate_id == candidate_id
            )
        ).all()
    )
    lineages: list[DatasetLineage] = []
    source_qualities: list[float | None] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.evidence_type] = counts.get(row.evidence_type, 0) + 1
        source_qualities.append(
            _source_quality_from_metadata(row.metadata_json)
        )
        lineages.append(_lineage_from_metadata(row.metadata_json))
    quality = (
        round(
            sum(value for value in source_qualities if value is not None)
            / len(source_qualities),
            6,
        )
        if source_qualities
        and all(value is not None for value in source_qualities)
        else None
    )
    return _combine_lineages(lineages), quality, counts


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _relationship_lineage(row: RelationshipRow) -> DatasetLineage:
    return _lineage_from_metadata(row.metadata_json)


def _owner_confirmed_relationship(row: RelationshipRow) -> bool:
    metadata = row.metadata_json or {}
    return bool(
        metadata.get("owner_confirmed") is True
        or str(metadata.get("decision_kind") or "").upper()
        == "OWNER_CONFIRMED"
    )


def collect_dataset_readiness_snapshot(
    session: Session,
    *,
    tenant_id: str,
) -> DatasetReadinessSnapshot:
    tenant = session.get(TenantRow, tenant_id)
    if tenant is None:
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_READINESS_TENANT_NOT_FOUND"
        )

    records: list[DatasetLabelRecord] = []
    evidence_type_counts: dict[str, int] = {}

    entity_candidates = list(
        session.scalars(
            select(EntityResolutionCandidateRow).where(
                EntityResolutionCandidateRow.tenant_id == tenant_id,
                EntityResolutionCandidateRow.state.in_(
                    {"CONFIRMED", "REJECTED"}
                ),
                EntityResolutionCandidateRow.decided_at.is_not(None),
                EntityResolutionCandidateRow.decision_actor_key.is_not(None),
            )
        ).all()
    )
    entity_by_id = {row.id: row for row in entity_candidates}
    for row in entity_candidates:
        lineage, quality, counts = _entity_resolution_lineage(
            session,
            candidate_id=row.id,
        )
        _merge_counts(evidence_type_counts, counts)
        records.append(
            DatasetLabelRecord(
                label_id=f"entity-resolution:{row.id}",
                label_kind="ENTITY_RESOLUTION",
                label_state=row.state,
                decided_at=_utc(row.decided_at),
                valid_from=None,
                valid_until=None,
                lineage=lineage,
                source_quality=quality,
                tenant_id=tenant_id,
            )
        )

    alias_corrections = list(
        session.scalars(
            select(EntityAliasResolutionRow).where(
                EntityAliasResolutionRow.tenant_id == tenant_id,
                EntityAliasResolutionRow.state == "REVOKED",
                EntityAliasResolutionRow.revoked_at.is_not(None),
                EntityAliasResolutionRow.revoked_by_actor_key.is_not(None),
            )
        ).all()
    )
    for row in alias_corrections:
        candidate = entity_by_id.get(row.candidate_id)
        if candidate is None:
            candidate = session.get(
                EntityResolutionCandidateRow,
                row.candidate_id,
            )
        if candidate is None or candidate.tenant_id != tenant_id:
            lineage = DatasetLineage.UNKNOWN
            quality = None
            counts = {}
        else:
            lineage, quality, counts = _entity_resolution_lineage(
                session,
                candidate_id=candidate.id,
            )
        _merge_counts(evidence_type_counts, counts)
        records.append(
            DatasetLabelRecord(
                label_id=f"entity-alias-revocation:{row.id}",
                label_kind="ENTITY_ALIAS_CORRECTION",
                label_state="REVOKED",
                decided_at=_utc(row.revoked_at),
                valid_from=None,
                valid_until=None,
                lineage=lineage,
                source_quality=quality,
                tenant_id=tenant_id,
            )
        )

    insights = list(
        session.scalars(
            select(CandidateInsightRow).where(
                CandidateInsightRow.tenant_id == tenant_id,
                CandidateInsightRow.state.in_(
                    {"ADMITTED", "REJECTED", "SUPERSEDED"}
                ),
                CandidateInsightRow.decision_actor_key.is_not(None),
            )
        ).all()
    )
    for row in insights:
        decided_at = row.decided_at or row.updated_at
        lineage, quality, counts = _candidate_evidence_lineage(
            session,
            tenant_id=tenant_id,
            candidate_id=row.id,
        )
        _merge_counts(evidence_type_counts, counts)
        records.append(
            DatasetLabelRecord(
                label_id=f"candidate-insight:{row.id}",
                label_kind=f"CANDIDATE_{row.insight_type}",
                label_state=row.state,
                decided_at=_utc(decided_at),
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
                lineage=lineage,
                source_quality=quality,
                tenant_id=tenant_id,
            )
        )

    relationships = list(
        session.scalars(
            select(RelationshipRow).where(
                RelationshipRow.tenant_id == tenant_id
            )
        ).all()
    )
    explicit_relationship_count = len(relationships)
    owner_confirmed_relationship_count = sum(
        1 for row in relationships if _owner_confirmed_relationship(row)
    )
    for row in relationships:
        lineage = _relationship_lineage(row)
        key = f"RELATIONSHIP_LINEAGE_{lineage.value}"
        evidence_type_counts[key] = evidence_type_counts.get(key, 0) + 1

    inferred_candidate_count = len(
        list(
            session.scalars(
                select(CandidateInsightRow.id).where(
                    CandidateInsightRow.tenant_id == tenant_id,
                    CandidateInsightRow.state.in_(
                        {"PROPOSED", "NEEDS_REVIEW"}
                    ),
                )
            ).all()
        )
    )

    records.sort(
        key=lambda item: (
            item.decided_at,
            item.label_kind,
            item.label_id,
        )
    )

    positive_states = {"CONFIRMED", "ADMITTED"}
    negative_states = {"REJECTED"}
    correction_states = {"SUPERSEDED", "REVOKED"}

    positive_label_count = sum(
        1 for row in records if row.label_state in positive_states
    )
    negative_label_count = sum(
        1 for row in records if row.label_state in negative_states
    )
    correction_label_count = sum(
        1 for row in records if row.label_state in correction_states
    )
    organic_label_count = sum(
        1 for row in records if row.lineage is DatasetLineage.ORGANIC
    )
    synthetic_label_count = sum(
        1 for row in records if row.lineage is DatasetLineage.SYNTHETIC
    )
    unknown_lineage_label_count = sum(
        1 for row in records if row.lineage is DatasetLineage.UNKNOWN
    )
    unknown_source_quality_label_count = sum(
        1 for row in records if row.source_quality is None
    )
    known_quality_records = [
        row for row in records if row.source_quality is not None
    ]
    mean_source_quality = (
        sum(
            float(row.source_quality)
            for row in known_quality_records
        )
        / len(known_quality_records)
        if known_quality_records
        else None
    )
    organic_records = [
        row for row in records
        if (
            row.lineage is DatasetLineage.ORGANIC
            and row.source_quality is not None
        )
    ]
    organic_mean_source_quality = (
        sum(float(row.source_quality) for row in organic_records)
        / len(organic_records)
        if organic_records
        else None
    )

    earliest = records[0].decided_at if records else None
    latest = records[-1].decided_at if records else None
    temporal_span_days = (
        max(0.0, (latest - earliest).total_seconds() / 86400.0)
        if earliest is not None and latest is not None
        else 0.0
    )

    fingerprint_payload = {
        "tenant_id": tenant_id,
        "substrate_version": SUBSTRATE_VERSION,
        "labels": [
            {
                "label_id": row.label_id,
                "label_kind": row.label_kind,
                "label_state": row.label_state,
                "decided_at": row.decided_at.isoformat(),
                "valid_from": (
                    row.valid_from.isoformat()
                    if row.valid_from is not None
                    else None
                ),
                "valid_until": (
                    row.valid_until.isoformat()
                    if row.valid_until is not None
                    else None
                ),
                "lineage": row.lineage.value,
                "source_quality": row.source_quality,
            }
            for row in records
        ],
        "explicit_relationship_count": explicit_relationship_count,
        "owner_confirmed_relationship_count": (
            owner_confirmed_relationship_count
        ),
        "evidence_type_counts": dict(sorted(evidence_type_counts.items())),
    }

    return DatasetReadinessSnapshot(
        tenant_id=tenant_id,
        dataset_fingerprint=stable_hash(fingerprint_payload),
        label_records=tuple(records),
        positive_label_count=positive_label_count,
        negative_label_count=negative_label_count,
        correction_label_count=correction_label_count,
        organic_label_count=organic_label_count,
        synthetic_label_count=synthetic_label_count,
        unknown_lineage_label_count=unknown_lineage_label_count,
        unknown_source_quality_label_count=(
            unknown_source_quality_label_count
        ),
        mean_source_quality=(
            round(mean_source_quality, 6)
            if mean_source_quality is not None
            else None
        ),
        organic_mean_source_quality=(
            round(organic_mean_source_quality, 6)
            if organic_mean_source_quality is not None
            else None
        ),
        explicit_relationship_count=explicit_relationship_count,
        owner_confirmed_relationship_count=owner_confirmed_relationship_count,
        inferred_candidate_count=inferred_candidate_count,
        earliest_decision_at=earliest,
        latest_decision_at=latest,
        temporal_span_days=round(temporal_span_days, 6),
        evidence_type_counts=dict(sorted(evidence_type_counts.items())),
    )


def _validate_policy(policy: LearnedGraphTaskPolicy) -> None:
    if not policy.justification.strip():
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_TASK_JUSTIFICATION_REQUIRED"
        )
    if not policy.deterministic_baseline_name.strip():
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_BASELINE_REQUIRED"
        )
    if not policy.deterministic_baseline_version.strip():
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_BASELINE_VERSION_REQUIRED"
        )
    if not policy.primary_metric.strip():
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_PRIMARY_METRIC_REQUIRED"
        )
    integer_values = (
        policy.min_positive_labels,
        policy.min_negative_labels,
        policy.min_correction_labels,
        policy.min_organic_labels,
        policy.min_temporal_span_days,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in integer_values
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_POLICY_MINIMUM_INVALID"
        )
    bounded_values = (
        policy.min_organic_source_quality,
        policy.max_unknown_lineage_fraction,
        policy.max_unknown_source_quality_fraction,
        policy.max_false_positive_rate,
        policy.max_calibration_error,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or value > 1
        for value in bounded_values
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_POLICY_BUDGET_INVALID"
        )
    if (
        isinstance(policy.minimum_baseline_improvement, bool)
        or not isinstance(
            policy.minimum_baseline_improvement,
            (int, float),
        )
        or not math.isfinite(
            float(policy.minimum_baseline_improvement)
        )
        or policy.minimum_baseline_improvement < 0
        or policy.minimum_baseline_improvement > 1
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_POLICY_IMPROVEMENT_INVALID"
        )


def _dataset_reasons(
    snapshot: DatasetReadinessSnapshot,
    policy: LearnedGraphTaskPolicy,
) -> list[str]:
    reasons: list[str] = []
    if snapshot.positive_label_count < policy.min_positive_labels:
        reasons.append("INSUFFICIENT_POSITIVE_LABELS")
    if snapshot.negative_label_count < policy.min_negative_labels:
        reasons.append("INSUFFICIENT_NEGATIVE_LABELS")
    if snapshot.correction_label_count < policy.min_correction_labels:
        reasons.append("INSUFFICIENT_CORRECTION_LABELS")
    if snapshot.organic_label_count < policy.min_organic_labels:
        reasons.append("INSUFFICIENT_ORGANIC_LABELS")
    if snapshot.temporal_span_days < policy.min_temporal_span_days:
        reasons.append("INSUFFICIENT_TEMPORAL_SPAN")
    if (
        snapshot.organic_mean_source_quality is None
        or snapshot.organic_mean_source_quality
        < policy.min_organic_source_quality
    ):
        reasons.append("INSUFFICIENT_ORGANIC_SOURCE_QUALITY")

    total = len(snapshot.label_records)
    unknown_fraction = (
        snapshot.unknown_lineage_label_count / total if total else 1.0
    )
    if unknown_fraction > policy.max_unknown_lineage_fraction:
        reasons.append("UNKNOWN_LINEAGE_BUDGET_EXCEEDED")
    unknown_quality_fraction = (
        snapshot.unknown_source_quality_label_count / total
        if total
        else 1.0
    )
    if (
        unknown_quality_fraction
        > policy.max_unknown_source_quality_fraction
    ):
        reasons.append("UNKNOWN_SOURCE_QUALITY_BUDGET_EXCEEDED")
    return reasons


def _split_ids(
    evaluation: OfflineEvaluationResult,
) -> tuple[set[str], set[str], set[str]]:
    train = set(evaluation.train_label_ids)
    validation = set(evaluation.validation_label_ids)
    test = set(evaluation.test_label_ids)
    if (
        len(train) != len(evaluation.train_label_ids)
        or len(validation) != len(evaluation.validation_label_ids)
        or len(test) != len(evaluation.test_label_ids)
    ):
        raise LearnedGraphReadinessError(
            "LEARNED_GRAPH_EVALUATION_DUPLICATE_LABEL"
        )
    return train, validation, test


def _evaluation_reasons(
    snapshot: DatasetReadinessSnapshot,
    policy: LearnedGraphTaskPolicy,
    evaluation: OfflineEvaluationResult,
) -> list[str]:
    reasons: list[str] = []

    if evaluation.task is not policy.task:
        reasons.append("EVALUATION_TASK_MISMATCH")
    if evaluation.tenant_id != snapshot.tenant_id:
        reasons.append("EVALUATION_TENANT_MISMATCH")
    if evaluation.dataset_fingerprint != snapshot.dataset_fingerprint:
        reasons.append("EVALUATION_DATASET_FINGERPRINT_MISMATCH")
    if not evaluation.reproducibility_ref.strip():
        reasons.append("EVALUATION_REPRODUCIBILITY_REF_REQUIRED")
    if (
        evaluation.deterministic_baseline_name
        != policy.deterministic_baseline_name
        or evaluation.deterministic_baseline_version
        != policy.deterministic_baseline_version
    ):
        reasons.append("EVALUATION_BASELINE_MISMATCH")
    if evaluation.primary_metric != policy.primary_metric:
        reasons.append("EVALUATION_PRIMARY_METRIC_MISMATCH")

    numeric_scores = (
        evaluation.baseline_score,
        evaluation.learned_score,
        evaluation.false_positive_rate,
        evaluation.calibration_error,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or value > 1
        for value in numeric_scores
    ):
        reasons.append("EVALUATION_METRIC_OUT_OF_RANGE")

    if evaluation.false_positive_rate > policy.max_false_positive_rate:
        reasons.append("FALSE_POSITIVE_BUDGET_EXCEEDED")
    if evaluation.calibration_error > policy.max_calibration_error:
        reasons.append("CALIBRATION_BUDGET_EXCEEDED")

    if policy.higher_is_better:
        beats_baseline = (
            evaluation.learned_score
            > evaluation.baseline_score
            + policy.minimum_baseline_improvement
        )
    else:
        beats_baseline = (
            evaluation.learned_score
            < evaluation.baseline_score
            - policy.minimum_baseline_improvement
        )
    if not beats_baseline:
        reasons.append("LEARNED_MODEL_DOES_NOT_BEAT_BASELINE")

    if policy.require_temporal_holdout and not evaluation.temporal_holdout:
        reasons.append("TEMPORAL_HOLDOUT_REQUIRED")
    if not evaluation.leakage_checks_passed:
        reasons.append("LEAKAGE_CHECK_FAILED")
    if not evaluation.explainability_path_available:
        reasons.append("EXPLAINABILITY_PATH_REQUIRED")
    if not evaluation.shadow_mode:
        reasons.append("SHADOW_MODE_REQUIRED")
    if not evaluation.test_lineage_organic_only:
        reasons.append("ORGANIC_TEST_LINEAGE_REQUIRED")

    try:
        train, validation, test = _split_ids(evaluation)
    except LearnedGraphReadinessError:
        reasons.append("EVALUATION_DUPLICATE_LABEL")
        return reasons

    if not train or not validation or not test:
        reasons.append("EVALUATION_SPLIT_EMPTY")
    if train & validation or train & test or validation & test:
        reasons.append("EVALUATION_SPLIT_LEAKAGE")

    by_id = {row.label_id: row for row in snapshot.label_records}
    all_ids = train | validation | test
    if any(label_id not in by_id for label_id in all_ids):
        reasons.append("EVALUATION_LABEL_NOT_IN_SNAPSHOT")
        return reasons

    train_end = _utc(evaluation.train_end)
    validation_end = _utc(evaluation.validation_end)
    test_start = _utc(evaluation.test_start)
    if not train_end < validation_end < test_start:
        reasons.append("EVALUATION_TEMPORAL_BOUNDARY_INVALID")

    if policy.require_temporal_holdout:
        if any(by_id[label_id].decided_at > train_end for label_id in train):
            reasons.append("TRAIN_LABEL_AFTER_TRAIN_END")
        if any(
            not (
                train_end
                < by_id[label_id].decided_at
                <= validation_end
            )
            for label_id in validation
        ):
            reasons.append("VALIDATION_LABEL_OUTSIDE_WINDOW")
        if any(
            by_id[label_id].decided_at < test_start
            for label_id in test
        ):
            reasons.append("TEST_LABEL_BEFORE_TEST_START")

    if any(
        by_id[label_id].lineage is not DatasetLineage.ORGANIC
        for label_id in test
    ):
        reasons.append("TEST_SET_NOT_ORGANIC")

    return reasons


def assess_learned_graph_readiness(
    session: Session,
    *,
    tenant_id: str,
    policy: LearnedGraphTaskPolicy | None = None,
    evaluation: OfflineEvaluationResult | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> LearnedGraphReadinessReport:
    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=tenant_id,
    )
    configured = settings or Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "learned-graph-readiness-internal-secret"
        ),
    )
    generated_at = _utc(now or datetime.now(UTC))

    if policy is None:
        return LearnedGraphReadinessReport(
            state=LearnedGraphReadinessState.NOT_READY,
            tenant_id=tenant_id,
            substrate_version=SUBSTRATE_VERSION,
            dataset=snapshot,
            task=None,
            feature_flag_enabled=(
                configured.learned_graph_intelligence_enabled
            ),
            shadow_mode_configured=configured.learned_graph_shadow_mode,
            reasons=("TASK_POLICY_REQUIRED",),
            evaluation_fingerprint=None,
            generated_at=generated_at,
        )

    _validate_policy(policy)
    reasons = _dataset_reasons(snapshot, policy)
    if reasons:
        return LearnedGraphReadinessReport(
            state=LearnedGraphReadinessState.NOT_READY,
            tenant_id=tenant_id,
            substrate_version=SUBSTRATE_VERSION,
            dataset=snapshot,
            task=policy.task,
            feature_flag_enabled=(
                configured.learned_graph_intelligence_enabled
            ),
            shadow_mode_configured=configured.learned_graph_shadow_mode,
            reasons=tuple(sorted(set(reasons))),
            evaluation_fingerprint=None,
            generated_at=generated_at,
        )

    if evaluation is None:
        return LearnedGraphReadinessReport(
            state=(
                LearnedGraphReadinessState.READY_FOR_OFFLINE_EVALUATION
            ),
            tenant_id=tenant_id,
            substrate_version=SUBSTRATE_VERSION,
            dataset=snapshot,
            task=policy.task,
            feature_flag_enabled=(
                configured.learned_graph_intelligence_enabled
            ),
            shadow_mode_configured=configured.learned_graph_shadow_mode,
            reasons=("OFFLINE_EVALUATION_REQUIRED",),
            evaluation_fingerprint=None,
            generated_at=generated_at,
        )

    evaluation_reasons = _evaluation_reasons(
        snapshot,
        policy,
        evaluation,
    )
    evaluation_fingerprint = stable_hash(
        {
            "task": evaluation.task.value,
            "tenant_id": evaluation.tenant_id,
            "dataset_fingerprint": evaluation.dataset_fingerprint,
            "reproducibility_ref": evaluation.reproducibility_ref,
            "deterministic_baseline_name": (
                evaluation.deterministic_baseline_name
            ),
            "deterministic_baseline_version": (
                evaluation.deterministic_baseline_version
            ),
            "primary_metric": evaluation.primary_metric,
            "baseline_score": evaluation.baseline_score,
            "learned_score": evaluation.learned_score,
            "false_positive_rate": evaluation.false_positive_rate,
            "calibration_error": evaluation.calibration_error,
            "train_label_ids": list(evaluation.train_label_ids),
            "validation_label_ids": list(
                evaluation.validation_label_ids
            ),
            "test_label_ids": list(evaluation.test_label_ids),
            "train_end": _utc(evaluation.train_end).isoformat(),
            "validation_end": _utc(
                evaluation.validation_end
            ).isoformat(),
            "test_start": _utc(evaluation.test_start).isoformat(),
            "temporal_holdout": evaluation.temporal_holdout,
            "test_lineage_organic_only": (
                evaluation.test_lineage_organic_only
            ),
            "leakage_checks_passed": evaluation.leakage_checks_passed,
            "explainability_path_available": (
                evaluation.explainability_path_available
            ),
            "shadow_mode": evaluation.shadow_mode,
        }
    )
    if evaluation_reasons:
        return LearnedGraphReadinessReport(
            state=LearnedGraphReadinessState.NOT_READY,
            tenant_id=tenant_id,
            substrate_version=SUBSTRATE_VERSION,
            dataset=snapshot,
            task=policy.task,
            feature_flag_enabled=(
                configured.learned_graph_intelligence_enabled
            ),
            shadow_mode_configured=configured.learned_graph_shadow_mode,
            reasons=tuple(sorted(set(evaluation_reasons))),
            evaluation_fingerprint=evaluation_fingerprint,
            generated_at=generated_at,
        )

    return LearnedGraphReadinessReport(
        state=LearnedGraphReadinessState.READY_FOR_SHADOW_TRIAL,
        tenant_id=tenant_id,
        substrate_version=SUBSTRATE_VERSION,
        dataset=snapshot,
        task=policy.task,
        feature_flag_enabled=configured.learned_graph_intelligence_enabled,
        shadow_mode_configured=configured.learned_graph_shadow_mode,
        reasons=("PRODUCTION_LEARNED_RUNTIME_REMAINS_DISABLED",),
        evaluation_fingerprint=evaluation_fingerprint,
        generated_at=generated_at,
    )


__all__ = [
    "LearnedGraphReadinessError",
    "SUBSTRATE_VERSION",
    "assess_learned_graph_readiness",
    "collect_dataset_readiness_snapshot",
]
