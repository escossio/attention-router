from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.application import cognitive_runtime
from attention_router.application.cognitive_runtime import (
    run_cognitive_runtime_cycle,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure import worker
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightRow,
)
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    ExecutionIntentRow,
    OutboxMessageRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


def _tenant(session) -> None:
    if session.get(TenantRow, DEFAULT_TENANT_ID) is not None:
        return
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    session.add(
        TenantRow(
            id=DEFAULT_TENANT_ID,
            slug="cognitive-runtime",
            name="Cognitive Runtime",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _world(
    session,
    *,
    suffix: str,
    lineage: str,
    stamp: datetime,
):
    _tenant(session)
    actor_key = f"cognitive-person-{suffix}"
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id=f"external-{suffix}",
        actor_key=actor_key,
        actor_category="contact",
        display_name=f"Pessoa {suffix}",
        metadata={},
        tenant_id=DEFAULT_TENANT_ID,
    )
    resource = ResourceRow(
        id=f"cognitive-resource-{suffix}",
        tenant_id=DEFAULT_TENANT_ID,
        resource_type="PROPERTY",
        canonical_name=f"Recurso {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(resource)
    session.flush()

    for index in range(2):
        canonical = CanonicalEventRow(
            id=f"canonical-{suffix}-{index}",
            tenant_id=DEFAULT_TENANT_ID,
            origin="TEST",
            event_type="PROPERTY_ACTIVITY",
            actor_id=actor_key,
            resource_id=resource.id,
            channel="test",
            payload_type="TEST",
            payload_ref={},
            occurred_at=stamp + timedelta(minutes=index),
            received_at=stamp + timedelta(minutes=index),
            correlation_id=f"corr-{suffix}-{index}",
            causation_id=None,
            inbound_event_id=None,
            metadata_sanitized={},
            lineage_classification=lineage,
            scenario_run_id=None,
            scenario_step_run_id=None,
        )
        session.add(canonical)
        session.flush()
        session.add(
            TimelineEventRow(
                id=f"timeline-{suffix}-{index}",
                tenant_id=DEFAULT_TENANT_ID,
                canonical_event_id=canonical.id,
                actor_id=actor_key,
                relationship_id=None,
                resource_id=resource.id,
                event_type="PROPERTY_ACTIVITY",
                event_ref={"source": "cognitive-runtime-test"},
                occurred_at=canonical.occurred_at,
                visibility="PRIVATE",
                provenance="canonical_event_bridge",
                metadata_json={},
            )
        )
    session.flush()
    return actor_key, resource


def test_cognitive_runtime_uses_only_organic_timeline_and_replays_idempotently(
    session,
):
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    _organic_actor, organic_resource = _world(
        session,
        suffix="organic",
        lineage="ORGANIC",
        stamp=stamp,
    )
    _synthetic_actor, synthetic_resource = _world(
        session,
        suffix="synthetic",
        lineage="SYNTHETIC",
        stamp=stamp + timedelta(hours=1),
    )

    first = run_cognitive_runtime_cycle(
        session,
        tenant_limit=10,
        graph_limit_per_kind=50,
        candidate_limit=10,
        now=stamp + timedelta(hours=2),
    )

    assert first.tenants_considered == 1
    assert first.tenants_succeeded == 1
    assert first.tenants_failed == 0
    assert first.relation_candidates == 1
    assert first.candidate_insights_created == 1
    assert first.candidate_insights_reused == 0

    candidate = session.scalar(select(CandidateInsightRow))
    assert candidate is not None
    assert candidate.source_engine == "RULE"
    assert candidate.state == "PROPOSED"
    assert candidate.insight_type == "RELATIONSHIP_PROPOSAL"
    assert candidate.proposed_value["target_id"] == organic_resource.id
    assert candidate.proposed_value["target_id"] != synthetic_resource.id
    assert candidate.provenance["governance"] == "CANDIDATE_ONLY"
    assert candidate.provenance["grants_authority"] is False
    assert candidate.provenance["materializes_canonical_truth"] is False

    second = run_cognitive_runtime_cycle(
        session,
        tenant_limit=10,
        graph_limit_per_kind=50,
        candidate_limit=10,
        now=stamp + timedelta(hours=3),
    )
    assert second.relation_candidates == 1
    assert second.candidate_insights_created == 0
    assert second.candidate_insights_reused == 1
    assert session.scalar(
        select(func.count()).select_from(CandidateInsightRow)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(RelationshipRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(ExecutionIntentRow)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(OutboxMessageRow)
    ) == 0


def test_cognitive_runtime_settings_default_off_and_bounded():
    configured = Settings(_env_file=None)

    assert configured.cognitive_runtime_enabled is False
    assert configured.cognitive_runtime_canary_tenant_id is None
    assert configured.cognitive_runtime_interval_seconds == 300
    assert configured.cognitive_runtime_tenant_limit == 50
    assert configured.cognitive_runtime_graph_limit_per_kind == 200
    assert configured.cognitive_runtime_candidate_limit == 24


def test_worker_cognitive_runtime_schedule_respects_flag_and_interval(
    session,
    monkeypatch,
):
    calls = []

    def fake_cycle(*args, **kwargs):
        calls.append(kwargs)
        from attention_router.application.cognitive_runtime import (
            CognitiveRuntimeCycleResult,
        )

        return CognitiveRuntimeCycleResult(tenants_considered=1)

    monkeypatch.setattr(worker, "run_cognitive_runtime_cycle", fake_cycle)
    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_enabled",
        False,
    )

    result, last = worker.process_cognitive_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is None
    assert last is None
    assert calls == []

    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_interval_seconds",
        300,
    )
    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_tenant_limit",
        7,
    )
    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_graph_limit_per_kind",
        80,
    )
    monkeypatch.setattr(
        worker.settings,
        "cognitive_runtime_candidate_limit",
        12,
    )

    result, last = worker.process_cognitive_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is not None
    assert last == 100.0
    assert calls[-1]["tenant_limit"] == 7
    assert calls[-1]["graph_limit_per_kind"] == 80
    assert calls[-1]["candidate_limit"] == 12

    result, same_last = worker.process_cognitive_runtime_if_due(
        session,
        now_monotonic=200.0,
        last_run_monotonic=last,
    )
    assert result is None
    assert same_last == last
    assert len(calls) == 1


def test_cognitive_runtime_rollback_does_not_inflate_committed_metrics(
    session,
    monkeypatch,
):
    stamp = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    _world(
        session,
        suffix="rollback",
        lineage="ORGANIC",
        stamp=stamp,
    )
    original = cognitive_runtime.persist_relation_candidate_insight

    def persist_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic tenant stage failure")

    monkeypatch.setattr(
        cognitive_runtime,
        "persist_relation_candidate_insight",
        persist_then_fail,
    )

    result = run_cognitive_runtime_cycle(
        session,
        tenant_limit=10,
        graph_limit_per_kind=50,
        candidate_limit=10,
        now=stamp + timedelta(hours=1),
    )

    assert result.tenants_considered == 1
    assert result.tenants_succeeded == 0
    assert result.tenants_failed == 1
    assert result.relation_candidates == 0
    assert result.candidate_insights_created == 0
    assert result.candidate_insights_reused == 0
    assert session.scalar(
        select(func.count()).select_from(CandidateInsightRow)
    ) == 0


def test_cognitive_runtime_exact_canary_tenant_skips_other_active_tenants(
    session,
):
    stamp = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
    _world(
        session,
        suffix="non-canary-organic",
        lineage="ORGANIC",
        stamp=stamp,
    )
    canary_tenant = "00000000-0000-4000-8000-000000000251"
    session.add(
        TenantRow(
            id=canary_tenant,
            slug="cognitive-canary-251",
            name="Cognitive Canary 251",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    result = run_cognitive_runtime_cycle(
        session,
        tenant_limit=10,
        canary_tenant_id=canary_tenant,
        graph_limit_per_kind=50,
        candidate_limit=10,
        now=stamp + timedelta(hours=1),
    )

    assert result.tenants_considered == 1
    assert result.tenants_succeeded == 1
    assert result.relation_candidates == 0
    assert session.scalar(
        select(func.count()).select_from(CandidateInsightRow)
    ) == 0
