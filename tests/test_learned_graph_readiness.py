from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.learned_graph_readiness import (
    LearnedGraphReadinessError,
    assess_learned_graph_readiness,
    collect_dataset_readiness_snapshot,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.learned_graph_readiness import (
    DatasetLineage,
    LearnedGraphReadinessState,
    LearnedGraphTask,
    LearnedGraphTaskPolicy,
    OfflineEvaluationResult,
)
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
    CandidateInsightRow,
)
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)


OWNER = "actor-owner-v2j"


def _ensure_tenant(session, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    session.add(
        TenantRow(
            id=tenant_id,
            slug=f"v2j-{tenant_id[-8:]}",
            name=f"V2J {tenant_id[-8:]}",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _resource(session, suffix: str, *, tenant_id: str = DEFAULT_TENANT_ID):
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    row = ResourceRow(
        id=f"{tenant_id}:v2j-resource-{suffix}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"V2J Resource {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _label(
    session,
    *,
    suffix: str,
    state: str,
    decided_at: datetime,
    provenance: str = "WHATSAPP",
    tenant_id: str = DEFAULT_TENANT_ID,
    confidence: float = 0.9,
):
    _ensure_tenant(session, tenant_id)
    resource = _resource(session, suffix, tenant_id=tenant_id)
    event = TimelineEventRow(
        id=f"{tenant_id}:v2j-event-{suffix}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=f"{tenant_id}:actor-{suffix}",
        relationship_id=None,
        resource_id=resource.id,
        event_type="GRAPH_LABEL_EVIDENCE",
        event_ref={"source": suffix},
        occurred_at=decided_at - timedelta(hours=1),
        visibility="PRIVATE",
        provenance=provenance,
        metadata_json={},
    )
    session.add(event)
    session.flush()

    decision_kind = {
        "ADMITTED": "OWNER_ADMITTED",
        "REJECTED": "OWNER_REJECTED",
        "SUPERSEDED": "OWNER_SUPERSEDED",
    }[state]
    insight = CandidateInsightRow(
        id=f"{tenant_id}:v2j-insight-{suffix}",
        tenant_id=tenant_id,
        semantic_key=f"v2j-semantic-{suffix}",
        idempotency_key=f"v2j-idempotency-{suffix}",
        insight_type="RELATIONSHIP_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.graph.structural_association",
        proposed_value={
            "target_type": "PERSON",
            "target_id": f"{tenant_id}:actor-{suffix}",
        },
        source_engine="RULE",
        confidence=confidence,
        sensitivity_class="PRIVATE",
        valid_from=decided_at - timedelta(days=2),
        valid_until=decided_at + timedelta(days=30),
        contradiction_refs=[],
        state=state,
        supersedes_insight_id=None,
        decision_kind=decision_kind,
        decision_actor_key=OWNER,
        decision_ref=f"owner-decision:{suffix}",
        provenance={
            "governance": "CANDIDATE_ONLY",
            "grants_authority": False,
        },
        created_at=decided_at - timedelta(days=1),
        updated_at=decided_at,
        decided_at=decided_at,
    )
    session.add(insight)
    session.flush()

    evidence = CandidateInsightEvidenceRow(
        id=f"{tenant_id}:v2j-evidence-{suffix}",
        tenant_id=tenant_id,
        candidate_id=insight.id,
        idempotency_key=f"v2j-evidence-key-{suffix}",
        evidence_type="TIMELINE_EVENT",
        source_ref=event.id,
        independence_key=f"v2j-event:{event.id}",
        evidence_role="SUPPORT",
        confidence=confidence,
        sensitivity_class="PRIVATE",
        observed_at=event.occurred_at,
        provenance=(
            {}
            if provenance == "UNKNOWN"
            else {"source_quality": confidence}
        ),
        created_at=decided_at,
    )
    session.add(evidence)
    session.flush()
    return insight


def _organic_dataset(session):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    specs = [
        ("p1", "ADMITTED", 0),
        ("n1", "REJECTED", 2),
        ("c1", "SUPERSEDED", 4),
        ("p2", "ADMITTED", 6),
        ("n2", "REJECTED", 8),
        ("p3", "ADMITTED", 10),
    ]
    rows = [
        _label(
            session,
            suffix=suffix,
            state=state,
            decided_at=base + timedelta(days=days),
        )
        for suffix, state, days in specs
    ]

    stamp = base + timedelta(days=1)
    session.add(
        RelationshipRow(
            id=f"{DEFAULT_TENANT_ID}:v2j-owner-confirmed-rel",
            tenant_id=DEFAULT_TENANT_ID,
            source_entity_type="ACTOR",
            source_entity_id=f"{DEFAULT_TENANT_ID}:owner",
            target_entity_type="RESOURCE",
            target_entity_id=rows[0].subject_id,
            relationship_type="OWNS",
            status="ACTIVE",
            valid_from=stamp,
            valid_until=None,
            metadata_json={
                "owner_confirmed": True,
                "lineage_classification": "ORGANIC",
            },
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()
    return rows


def _policy() -> LearnedGraphTaskPolicy:
    return LearnedGraphTaskPolicy(
        task=LearnedGraphTask.LINK_PREDICTION,
        justification=(
            "Evaluate whether learned link prediction beats the certified "
            "V2I deterministic relation-candidate baseline."
        ),
        deterministic_baseline_name="DETERMINISTIC_RELATION_CANDIDATE",
        deterministic_baseline_version="V0",
        primary_metric="average_precision",
        higher_is_better=True,
        min_positive_labels=2,
        min_negative_labels=2,
        min_correction_labels=1,
        min_organic_labels=6,
        min_temporal_span_days=7,
        min_organic_source_quality=0.8,
        max_unknown_lineage_fraction=0.0,
        max_unknown_source_quality_fraction=0.0,
        max_false_positive_rate=0.10,
        max_calibration_error=0.10,
        minimum_baseline_improvement=0.01,
        require_temporal_holdout=True,
    )


def _evaluation(snapshot) -> OfflineEvaluationResult:
    records = list(snapshot.label_records)
    return OfflineEvaluationResult(
        task=LearnedGraphTask.LINK_PREDICTION,
        tenant_id=DEFAULT_TENANT_ID,
        dataset_fingerprint=snapshot.dataset_fingerprint,
        reproducibility_ref="eval-run:sha256:abcdef0123456789",
        deterministic_baseline_name="DETERMINISTIC_RELATION_CANDIDATE",
        deterministic_baseline_version="V0",
        primary_metric="average_precision",
        baseline_score=0.70,
        learned_score=0.78,
        false_positive_rate=0.05,
        calibration_error=0.04,
        train_label_ids=tuple(row.label_id for row in records[:2]),
        validation_label_ids=tuple(row.label_id for row in records[2:4]),
        test_label_ids=tuple(row.label_id for row in records[4:]),
        train_end=records[1].decided_at,
        validation_end=records[3].decided_at,
        test_start=records[4].decided_at,
        temporal_holdout=True,
        test_lineage_organic_only=True,
        leakage_checks_passed=True,
        explainability_path_available=True,
        shadow_mode=True,
    )


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret=(
            "v2j-test-internal-secret-that-is-long-enough"
        ),
        **overrides,
    )


def test_v2j_without_task_policy_is_not_ready(session):
    _ensure_tenant(session)

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        settings=_settings(),
    )

    assert report.state is LearnedGraphReadinessState.NOT_READY
    assert report.reasons == ("TASK_POLICY_REQUIRED",)
    assert report.feature_flag_enabled is False
    assert report.shadow_mode_configured is True
    assert report.ready_for_learned_runtime is False


def test_v2j_dataset_snapshot_distinguishes_lineage_quality_and_temporal_scope(
    session,
):
    organic = _label(
        session,
        suffix="organic",
        state="ADMITTED",
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance="WHATSAPP",
        confidence=0.95,
    )
    _label(
        session,
        suffix="synthetic",
        state="REJECTED",
        decided_at=datetime(2026, 1, 2, tzinfo=UTC),
        provenance="TEST",
        confidence=0.70,
    )
    _label(
        session,
        suffix="unknown",
        state="SUPERSEDED",
        decided_at=datetime(2026, 1, 3, tzinfo=UTC),
        provenance="UNKNOWN",
        confidence=0.50,
    )

    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )

    assert snapshot.organic_label_count == 1
    assert snapshot.synthetic_label_count == 1
    assert snapshot.unknown_lineage_label_count == 1
    assert snapshot.unknown_source_quality_label_count == 1
    assert snapshot.mean_source_quality == pytest.approx(
        (0.95 + 0.70) / 2,
        abs=1e-6,
    )
    assert snapshot.organic_mean_source_quality == pytest.approx(0.95)
    record = next(
        row
        for row in snapshot.label_records
        if row.label_id == f"candidate-insight:{organic.id}"
    )
    assert record.lineage is DatasetLineage.ORGANIC
    assert record.valid_from is not None
    assert record.valid_until is not None
    assert snapshot.temporal_span_days == pytest.approx(2.0)


def test_v2j_task_policy_blocks_unknown_lineage_and_insufficient_labels(session):
    _label(
        session,
        suffix="unknown-only",
        state="ADMITTED",
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance="UNKNOWN",
    )

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        settings=_settings(),
    )

    assert report.state is LearnedGraphReadinessState.NOT_READY
    assert {
        "INSUFFICIENT_POSITIVE_LABELS",
        "INSUFFICIENT_NEGATIVE_LABELS",
        "INSUFFICIENT_CORRECTION_LABELS",
        "INSUFFICIENT_ORGANIC_LABELS",
        "INSUFFICIENT_TEMPORAL_SPAN",
        "INSUFFICIENT_ORGANIC_SOURCE_QUALITY",
        "UNKNOWN_LINEAGE_BUDGET_EXCEEDED",
        "UNKNOWN_SOURCE_QUALITY_BUDGET_EXCEEDED",
    } <= set(report.reasons)


def test_v2j_sufficient_dataset_without_evaluation_is_only_offline_ready(session):
    _organic_dataset(session)

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        settings=_settings(),
    )

    assert (
        report.state
        is LearnedGraphReadinessState.READY_FOR_OFFLINE_EVALUATION
    )
    assert report.reasons == ("OFFLINE_EVALUATION_REQUIRED",)
    assert report.dataset.positive_label_count == 3
    assert report.dataset.negative_label_count == 2
    assert report.dataset.correction_label_count == 1
    assert report.dataset.organic_label_count == 6
    assert report.dataset.owner_confirmed_relationship_count == 1
    assert report.ready_for_learned_runtime is False


def test_v2j_reproducible_offline_evaluation_can_only_unlock_shadow_trial(session):
    _organic_dataset(session)
    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )
    evaluation = _evaluation(snapshot)

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        evaluation=evaluation,
        settings=_settings(),
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert (
        report.state
        is LearnedGraphReadinessState.READY_FOR_SHADOW_TRIAL
    )
    assert report.reasons == (
        "PRODUCTION_LEARNED_RUNTIME_REMAINS_DISABLED",
    )
    assert report.feature_flag_enabled is False
    assert report.shadow_mode_configured is True
    assert report.evaluation_fingerprint is not None
    assert report.ready_for_learned_runtime is False


def test_v2j_evaluation_fails_closed_on_split_leakage_and_nonorganic_test(session):
    _organic_dataset(session)
    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )
    base = _evaluation(snapshot)
    overlapping = replace(
        base,
        validation_label_ids=(
            base.train_label_ids[-1],
            *base.validation_label_ids,
        ),
        test_lineage_organic_only=False,
        leakage_checks_passed=False,
    )

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        evaluation=overlapping,
        settings=_settings(),
    )

    assert report.state is LearnedGraphReadinessState.NOT_READY
    assert "EVALUATION_SPLIT_LEAKAGE" in report.reasons
    assert "LEAKAGE_CHECK_FAILED" in report.reasons
    assert "ORGANIC_TEST_LINEAGE_REQUIRED" in report.reasons


def test_v2j_learned_model_must_beat_declared_deterministic_baseline(session):
    _organic_dataset(session)
    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )
    base = _evaluation(snapshot)
    weaker = replace(base, learned_score=0.705)

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        evaluation=weaker,
        settings=_settings(),
    )

    assert report.state is LearnedGraphReadinessState.NOT_READY
    assert "LEARNED_MODEL_DOES_NOT_BEAT_BASELINE" in report.reasons


def test_v2j_cross_tenant_evaluation_is_rejected(session):
    _organic_dataset(session)
    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )
    base = _evaluation(snapshot)
    foreign = replace(
        base,
        tenant_id="00000000-0000-4000-8000-00000000a229",
    )

    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        evaluation=foreign,
        settings=_settings(),
    )

    assert report.state is LearnedGraphReadinessState.NOT_READY
    assert "EVALUATION_TENANT_MISMATCH" in report.reasons


def test_v2j_readiness_assessment_is_read_only_and_non_actionable(session):
    _organic_dataset(session)
    before = {
        "candidate_insights": session.scalar(
            select(func.count()).select_from(CandidateInsightRow)
        ),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }

    snapshot = collect_dataset_readiness_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
    )
    report = assess_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        policy=_policy(),
        evaluation=_evaluation(snapshot),
        settings=_settings(),
    )

    assert (
        report.state
        is LearnedGraphReadinessState.READY_FOR_SHADOW_TRIAL
    )
    after = {
        "candidate_insights": session.scalar(
            select(func.count()).select_from(CandidateInsightRow)
        ),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
    }
    assert after == before
    assert report.ready_for_learned_runtime is False


def test_v2j_invalid_task_policy_fails_closed(session):
    _ensure_tenant(session)
    invalid = replace(_policy(), justification=" ")

    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_TASK_JUSTIFICATION_REQUIRED",
    ):
        assess_learned_graph_readiness(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            policy=invalid,
            settings=_settings(),
        )
