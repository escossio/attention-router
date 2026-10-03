from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.candidate_insights import (
    CandidateEvidenceInput,
    CandidateInsightError,
    admit_candidate_insight,
    propose_candidate_insight,
    reject_candidate_insight,
)
from attention_router.application.entity_resolution import (
    IdentityEvidenceInput,
    confirm_entity_resolution,
    propose_entity_resolution,
    reject_entity_resolution,
)
from attention_router.application.learned_graph_readiness import (
    LearnedGraphReadinessError,
    authorize_learned_graph_runtime_mode,
    build_temporal_holdout,
    collect_corrected_graph_labels,
    evaluate_learned_graph_readiness,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.learned_graph_readiness import (
    CorrectedGraphLabel,
    DatasetLineageClass,
    EvaluationMetrics,
    LabelValue,
    LearnedGraphReadinessPolicy,
    LearnedGraphRuntimeMode,
    LearnedGraphTask,
)
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


OWNER = "actor-owner-v2j"


def _owner(session):
    return upsert_actor_binding(
        session,
        source="test",
        external_actor_id="owner-v2j-external",
        actor_key=OWNER,
        actor_category="owner",
        display_name="Owner V2J",
        metadata={"owner": True},
        tenant_id=DEFAULT_TENANT_ID,
    )


def _resource(session, suffix: str) -> ResourceRow:
    stamp = now_utc()
    row = ResourceRow(
        id=f"v2j-resource-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
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


def _timeline_event(
    session,
    *,
    resource: ResourceRow,
    suffix: str,
    occurred_at: datetime,
    lineage: str,
) -> TimelineEventRow:
    canonical = CanonicalEventRow(
        id=f"v2j-canonical-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        origin="TEST",
        event_type="V2J_EVIDENCE",
        actor_id=OWNER,
        resource_id=resource.id,
        channel="test",
        payload_type="TEST",
        payload_ref={"suffix": suffix},
        occurred_at=occurred_at,
        received_at=occurred_at,
        correlation_id=f"v2j-correlation-{suffix}",
        causation_id=None,
        inbound_event_id=None,
        metadata_sanitized={},
        lineage_classification=lineage,
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    session.add(canonical)
    session.flush()

    event = TimelineEventRow(
        id=f"v2j-event-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        canonical_event_id=canonical.id,
        actor_id=OWNER,
        relationship_id=None,
        resource_id=resource.id,
        event_type="V2J_EVIDENCE",
        event_ref={"canonical_event_id": canonical.id},
        occurred_at=occurred_at,
        visibility="PRIVATE",
        provenance="TEST",
        metadata_json={},
    )
    session.add(event)
    session.flush()
    return event


def _link_label(
    session,
    *,
    index: int,
    positive: bool,
    lineage: str,
    decided_at: datetime,
):
    resource = _resource(session, f"link-{index}")
    event = _timeline_event(
        session,
        resource=resource,
        suffix=f"link-{index}",
        occurred_at=decided_at - timedelta(minutes=5),
        lineage=lineage,
    )
    candidate, created = propose_candidate_insight(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        insight_type="RELATIONSHIP_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate=f"context.v2j.link.{index}",
        proposed_value={
            "target_type": "PERSON",
            "target_id": OWNER,
            "relation_hint": "V2J_TEST",
        },
        source_engine="RULE",
        confidence=0.8,
        evidence=[
            CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT",
                source_ref=event.id,
                independence_key=f"v2j-event:{event.id}",
                confidence=0.9,
            )
        ],
        sensitivity_class="PRIVATE",
        valid_from=event.occurred_at,
        valid_until=decided_at + timedelta(days=1),
        now=decided_at - timedelta(minutes=4),
    )
    assert created is True
    if positive:
        return admit_candidate_insight(
            session,
            candidate_id=candidate.id,
            decision_actor_key=OWNER,
            decision_ref=f"owner-v2j-admit-{index}",
            now=decided_at,
        )
    return reject_candidate_insight(
        session,
        candidate_id=candidate.id,
        decision_actor_key=OWNER,
        decision_ref=f"owner-v2j-reject-{index}",
        now=decided_at,
    )


def _entity_label(
    session,
    *,
    index: int,
    positive: bool,
    lineage: str,
    decided_at: datetime,
):
    left = f"actor-v2j-entity-{index}-a"
    right = f"actor-v2j-entity-{index}-b"
    for actor_key, suffix in ((left, "a"), (right, "b")):
        upsert_actor_binding(
            session,
            source="test",
            external_actor_id=f"external-v2j-entity-{index}-{suffix}",
            actor_key=actor_key,
            actor_category="contact",
            display_name=f"Entity Actor {index} {suffix}",
            metadata={},
            tenant_id=DEFAULT_TENANT_ID,
        )

    candidate, created = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=left,
        actor_key_b=right,
        evidence=[
            IdentityEvidenceInput(
                evidence_type="SOURCE_ALIAS",
                source_ref=f"entity-label-{index}",
                confidence=0.9,
                independence_key=f"entity-label-{index}",
                metadata={"lineage_classification": lineage},
            )
        ],
        now=decided_at - timedelta(minutes=1),
    )
    assert created is True
    if positive:
        confirmed, _alias = confirm_entity_resolution(
            session,
            candidate_id=candidate.id,
            canonical_actor_key=left,
            decision_actor_key=OWNER,
            decision_ref=f"owner-v2j-entity-confirm-{index}",
            now=decided_at,
        )
        return confirmed
    return reject_entity_resolution(
        session,
        candidate_id=candidate.id,
        decision_actor_key=OWNER,
        decision_ref=f"owner-v2j-entity-reject-{index}",
        now=decided_at,
    )


def _organic_entity_dataset(session, *, count: int = 14):
    _owner(session)
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = []
    for index in range(count):
        rows.append(
            _entity_label(
                session,
                index=index,
                positive=index % 2 == 0,
                lineage="ORGANIC",
                decided_at=start + timedelta(days=index),
            )
        )
    return tuple(rows)


def _policy_for_small_fixture() -> LearnedGraphReadinessPolicy:
    return LearnedGraphReadinessPolicy(
        minimum_total_labels=14,
        minimum_positive_labels=7,
        minimum_negative_labels=7,
        minimum_organic_labels=14,
        maximum_unknown_lineage_ratio=0.0,
        minimum_temporal_span_days=13,
        minimum_test_examples=3,
        minimum_precision_improvement=0.02,
        maximum_false_positive_rate=0.05,
        maximum_calibration_error=0.10,
        minimum_explainability_coverage=0.95,
        maximum_recall_regression=0.02,
    )


def _metrics(
    *,
    task: LearnedGraphTask,
    learned: bool,
    split_fingerprint: str,
    false_positive_rate: float = 0.04,
) -> EvaluationMetrics:
    baseline_names = {
        LearnedGraphTask.LINK_PREDICTION: "DETERMINISTIC_RELATION_CANDIDATE",
        LearnedGraphTask.ENTITY_RESOLUTION: "DETERMINISTIC_ENTITY_RESOLUTION_V0",
    }
    learned_names = {
        LearnedGraphTask.LINK_PREDICTION: "RGCN_SHADOW_CANDIDATE",
        LearnedGraphTask.ENTITY_RESOLUTION: "ENTITY_EMBEDDING_SHADOW_CANDIDATE",
    }
    if learned:
        return EvaluationMetrics(
            task=task,
            engine_name=learned_names[task],
            engine_version="candidate-v0",
            precision=0.85,
            recall=0.81,
            false_positive_rate=false_positive_rate,
            calibration_error=0.07,
            explainability_coverage=0.98,
            test_examples=3,
            split_fingerprint=split_fingerprint,
            reproducibility_ref=f"eval:v2j:{task.value}:learned:001",
        )
    return EvaluationMetrics(
        task=task,
        engine_name=baseline_names[task],
        engine_version="V0",
        precision=0.80,
        recall=0.82,
        false_positive_rate=0.08,
        calibration_error=0.15,
        explainability_coverage=1.0,
        test_examples=3,
        split_fingerprint=split_fingerprint,
        reproducibility_ref=f"eval:v2j:{task.value}:baseline:001",
    )


def test_v2j_empty_dataset_is_not_ready_and_runtime_defaults_off(session):
    _owner(session)
    before = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
    }

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.LINK_PREDICTION,
    )

    assert report.ready_for_shadow is False
    assert report.runtime_mode_default == LearnedGraphRuntimeMode.OFF
    assert report.dataset.total == 0
    assert "MINIMUM_TOTAL_LABELS" in report.failed_gate_codes
    assert "BASELINE_METRICS_PRESENT" in report.failed_gate_codes
    assert "LEARNED_METRICS_PRESENT" in report.failed_gate_codes

    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_SHADOW_READINESS_REQUIRED",
    ):
        authorize_learned_graph_runtime_mode(
            report,
            requested_mode=LearnedGraphRuntimeMode.SHADOW,
            feature_flag_enabled=True,
        )
    assert authorize_learned_graph_runtime_mode(
        report,
        requested_mode=LearnedGraphRuntimeMode.OFF,
    ) == LearnedGraphRuntimeMode.OFF
    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_ACTIVE_MODE_NOT_ALLOWED_V2J",
    ):
        authorize_learned_graph_runtime_mode(
            report,
            requested_mode=LearnedGraphRuntimeMode.ACTIVE,
        )

    after = {
        "execution": session.scalar(
            select(func.count()).select_from(ExecutionIntentRow)
        ),
        "outbox": session.scalar(
            select(func.count()).select_from(OutboxMessageRow)
        ),
        "relationships": session.scalar(
            select(func.count()).select_from(RelationshipRow)
        ),
    }
    assert after == before


def test_v2j_organic_corrected_dataset_and_better_model_allow_shadow_only(
    session,
    monkeypatch,
):
    _organic_entity_dataset(session)
    labels, _, _, _ = collect_corrected_graph_labels(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    split = build_temporal_holdout(
        labels,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    assert split is not None
    assert all(
        item.source_quality == "EVIDENCE:SOURCE_ALIAS"
        for item in labels
    )

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
        baseline=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=False,
            split_fingerprint=split.fingerprint,
        ),
        learned=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=True,
            split_fingerprint=split.fingerprint,
        ),
        policy=_policy_for_small_fixture(),
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert report.dataset.total == 14
    assert report.dataset.positive == 7
    assert report.dataset.negative == 7
    assert report.dataset.organic == 14
    assert report.dataset.organic_positive == 7
    assert report.dataset.organic_negative == 7
    assert report.dataset.synthetic == 0
    assert report.dataset.historical_unknown == 0
    assert report.dataset.admitted_noncanonical == 0
    assert report.dataset.temporal_span_days == pytest.approx(13.0)
    assert report.split is not None
    assert len(report.split.train_label_ids) == 9
    assert len(report.split.validation_label_ids) == 2
    assert len(report.split.test_label_ids) == 3
    assert report.split.train_positive > 0
    assert report.split.train_negative > 0
    assert report.split.validation_positive > 0
    assert report.split.validation_negative > 0
    assert report.split.test_positive > 0
    assert report.split.test_negative > 0
    assert report.split.group_leakage_detected is False
    assert report.split.lineage_leakage_detected is False
    assert (
        report.split.train_until
        < report.split.validation_until
        < report.split.test_from
    )
    assert report.ready_for_shadow is True
    assert report.failed_gate_codes == ()

    monkeypatch.delenv("LEARNED_GRAPH_SHADOW_ENABLED", raising=False)
    assert Settings(_env_file=None).learned_graph_shadow_enabled is False
    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_SHADOW_FEATURE_FLAG_REQUIRED",
    ):
        authorize_learned_graph_runtime_mode(
            report,
            requested_mode=LearnedGraphRuntimeMode.SHADOW,
        )
    assert authorize_learned_graph_runtime_mode(
        report,
        requested_mode=LearnedGraphRuntimeMode.SHADOW,
        feature_flag_enabled=True,
    ) == LearnedGraphRuntimeMode.SHADOW
    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_ACTIVE_MODE_NOT_ALLOWED_V2J",
    ):
        authorize_learned_graph_runtime_mode(
            report,
            requested_mode=LearnedGraphRuntimeMode.ACTIVE,
            feature_flag_enabled=True,
        )


def test_v2j_admitted_relationship_candidate_is_not_positive_label(session):
    _owner(session)
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    _link_label(
        session,
        index=100,
        positive=True,
        lineage="ORGANIC",
        decided_at=start,
    )
    _link_label(
        session,
        index=101,
        positive=False,
        lineage="ORGANIC",
        decided_at=start + timedelta(days=1),
    )

    labels, superseded, pending, admitted = collect_corrected_graph_labels(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.LINK_PREDICTION,
    )

    assert superseded == 0
    assert pending == 0
    assert admitted == 1
    assert len(labels) == 1
    assert labels[0].value == LabelValue.NEGATIVE
    assert labels[0].source_quality == "ENGINE:RULE|EVIDENCE:TIMELINE_EVENT"
    assert labels[0].valid_from is not None
    assert labels[0].valid_until is not None

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.LINK_PREDICTION,
    )
    assert report.dataset.positive == 0
    assert report.dataset.admitted_noncanonical == 1
    assert report.ready_for_shadow is False
    assert (
        "MINIMUM_ORGANIC_POSITIVE_LABELS"
        in report.failed_gate_codes
    )


def test_v2j_synthetic_negatives_cannot_satisfy_organic_class_gate(session):
    _owner(session)
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for index in range(14):
        _entity_label(
            session,
            index=index,
            positive=True,
            lineage="ORGANIC",
            decided_at=start + timedelta(days=index),
        )
    for index in range(14, 21):
        _entity_label(
            session,
            index=index,
            positive=False,
            lineage="SYNTHETIC",
            decided_at=start + timedelta(days=index),
        )

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
        policy=_policy_for_small_fixture(),
    )

    assert report.dataset.negative == 7
    assert report.dataset.organic_negative == 0
    assert report.ready_for_shadow is False
    assert (
        "MINIMUM_ORGANIC_NEGATIVE_LABELS"
        in report.failed_gate_codes
    )


def test_v2j_false_positive_budget_blocks_shadow_even_when_precision_improves(session):
    _organic_entity_dataset(session)
    labels, _, _, _ = collect_corrected_graph_labels(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    split = build_temporal_holdout(
        labels,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    assert split is not None

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
        baseline=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=False,
            split_fingerprint=split.fingerprint,
        ),
        learned=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=True,
            split_fingerprint=split.fingerprint,
            false_positive_rate=0.08,
        ),
        policy=_policy_for_small_fixture(),
    )

    assert report.learned is not None
    assert report.baseline is not None
    assert report.learned.precision > report.baseline.precision
    assert report.ready_for_shadow is False
    assert "FALSE_POSITIVE_BUDGET" in report.failed_gate_codes


def test_v2j_same_test_count_with_wrong_split_fingerprint_fails(session):
    _organic_entity_dataset(session)
    labels, _, _, _ = collect_corrected_graph_labels(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    split = build_temporal_holdout(
        labels,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )
    assert split is not None

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
        baseline=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=False,
            split_fingerprint=split.fingerprint,
        ),
        learned=_metrics(
            task=LearnedGraphTask.ENTITY_RESOLUTION,
            learned=True,
            split_fingerprint="different-split-fingerprint",
        ),
        policy=_policy_for_small_fixture(),
    )

    assert report.ready_for_shadow is False
    assert (
        "EVALUATION_MATCHES_TEMPORAL_HOLDOUT"
        in report.failed_gate_codes
    )


def test_v2j_temporal_holdout_excludes_nonorganic_and_detects_group_leakage():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    labels = tuple(
        [
            CorrectedGraphLabel(
                tenant_id="tenant-v2j",
                task=LearnedGraphTask.LINK_PREDICTION,
                label_id=f"organic-{index}",
                group_key=(
                    "shared-group"
                    if index in {0, 5}
                    else f"group-{index}"
                ),
                value=(
                    LabelValue.POSITIVE
                    if index % 2 == 0
                    else LabelValue.NEGATIVE
                ),
                decided_at=start + timedelta(days=index),
                lineage=DatasetLineageClass.ORGANIC,
                source_kind="TEST",
                source_ref=f"ref-{index}",
                decision_kind="OWNER_DECISION",
                provenance={},
            )
            for index in range(6)
        ]
        + [
            CorrectedGraphLabel(
                tenant_id="tenant-v2j",
                task=LearnedGraphTask.LINK_PREDICTION,
                label_id="synthetic-1",
                group_key="synthetic-group",
                value=LabelValue.POSITIVE,
                decided_at=start + timedelta(days=20),
                lineage=DatasetLineageClass.SYNTHETIC,
                source_kind="TEST",
                source_ref="synthetic-ref",
                decision_kind="OWNER_DECISION",
                provenance={},
            )
        ]
    )

    split = build_temporal_holdout(
        labels,
        task=LearnedGraphTask.LINK_PREDICTION,
    )

    assert split is not None
    assert split.excluded_non_organic_label_ids == ("synthetic-1",)
    assert split.lineage_leakage_detected is False
    assert split.group_leakage_detected is True
    assert "synthetic-1" not in {
        *split.train_label_ids,
        *split.validation_label_ids,
        *split.test_label_ids,
    }


def test_v2j_entity_resolution_owner_decisions_become_task_specific_labels(session):
    _owner(session)
    actors = []
    for index in range(4):
        key = f"actor-v2j-{index}"
        upsert_actor_binding(
            session,
            source="test",
            external_actor_id=f"external-v2j-{index}",
            actor_key=key,
            actor_category="contact",
            display_name=f"Actor {index}",
            metadata={},
            tenant_id=DEFAULT_TENANT_ID,
        )
        actors.append(key)

    first, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=actors[0],
        actor_key_b=actors[1],
        evidence=[
            IdentityEvidenceInput(
                evidence_type="SOURCE_ALIAS",
                source_ref="entity-label-positive",
                confidence=0.9,
                independence_key="entity-positive",
                metadata={"lineage_classification": "ORGANIC"},
            )
        ],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    confirm_entity_resolution(
        session,
        candidate_id=first.id,
        canonical_actor_key=actors[0],
        decision_actor_key=OWNER,
        decision_ref="owner-v2j-entity-confirm",
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    second, _ = propose_entity_resolution(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key_a=actors[2],
        actor_key_b=actors[3],
        evidence=[
            IdentityEvidenceInput(
                evidence_type="SOURCE_ALIAS",
                source_ref="entity-label-negative",
                confidence=0.8,
                independence_key="entity-negative",
                metadata={"lineage_classification": "ORGANIC"},
            )
        ],
        now=datetime(2026, 1, 3, tzinfo=UTC),
    )
    reject_entity_resolution(
        session,
        candidate_id=second.id,
        decision_actor_key=OWNER,
        decision_ref="owner-v2j-entity-reject",
        now=datetime(2026, 1, 4, tzinfo=UTC),
    )

    labels, superseded, pending, admitted = collect_corrected_graph_labels(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.ENTITY_RESOLUTION,
    )

    assert superseded == 0
    assert pending == 0
    assert admitted == 0
    assert len(labels) == 2
    assert {item.value for item in labels} == {
        LabelValue.POSITIVE,
        LabelValue.NEGATIVE,
    }
    assert all(
        item.lineage == DatasetLineageClass.ORGANIC
        for item in labels
    )
    assert all(
        item.source_kind == "ENTITY_RESOLUTION_CANDIDATE"
        for item in labels
    )
    assert all(
        item.source_quality == "EVIDENCE:SOURCE_ALIAS"
        for item in labels
    )


def test_v2j_invalid_policy_cannot_bypass_readiness_gate(session):
    _owner(session)

    with pytest.raises(
        LearnedGraphReadinessError,
        match="LEARNED_GRAPH_READINESS_POLICY_LABEL_COUNTS_INVALID",
    ):
        evaluate_learned_graph_readiness(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            task=LearnedGraphTask.LINK_PREDICTION,
            policy=LearnedGraphReadinessPolicy(
                minimum_total_labels=1,
                minimum_positive_labels=1,
                minimum_negative_labels=1,
                minimum_organic_labels=1,
            ),
        )


def test_v2j_unsupported_task_fails_closed(session):
    _owner(session)

    report = evaluate_learned_graph_readiness(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        task=LearnedGraphTask.GRAPH_ANOMALY_SCORING,
    )

    assert report.ready_for_shadow is False
    assert "TASK_LABEL_ADAPTER_SUPPORTED" in report.failed_gate_codes


def test_v2j_readiness_does_not_remove_candidate_insight_gnn_gate(session):
    _owner(session)
    resource = _resource(session, "gnn-gate")
    event = _timeline_event(
        session,
        resource=resource,
        suffix="gnn-gate",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        lineage="ORGANIC",
    )

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_ENGINE_UNSUPPORTED",
    ):
        propose_candidate_insight(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            insight_type="RELATIONSHIP_PROPOSAL",
            subject_type="RESOURCE",
            subject_id=resource.id,
            predicate="context.v2j.gnn.still-gated",
            proposed_value={"target_id": OWNER},
            source_engine="GNN",
            confidence=0.99,
            evidence=[
                CandidateEvidenceInput(
                    evidence_type="TIMELINE_EVENT",
                    source_ref=event.id,
                    independence_key=f"event:{event.id}",
                    confidence=1.0,
                )
            ],
        )
