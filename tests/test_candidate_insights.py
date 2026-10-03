from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application.candidate_insights import (
    CandidateEvidenceInput,
    CandidateInsightError,
    admit_candidate_insight,
    consolidate_episode_scope,
    propose_candidate_insight,
    reject_candidate_insight,
    supersede_candidate_insight,
)
from attention_router.application.semantic_episodes import (
    assign_timeline_event_to_episode,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
    CandidateInsightRow,
)
from attention_router.infrastructure.models import (
    EntityStateRow,
    ExecutionIntentRow,
    FactRow,
    OutboxMessageRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding


OWNER = "actor-owner-v2e"


def _owner(session, *, tenant_id: str = DEFAULT_TENANT_ID):
    return upsert_actor_binding(
        session,
        source="whatsapp",
        external_actor_id=f"owner-{tenant_id[-6:]}",
        actor_key=OWNER if tenant_id == DEFAULT_TENANT_ID else f"{OWNER}-{tenant_id[-6:]}",
        actor_category="owner",
        display_name="Owner",
        metadata={"owner": True},
        tenant_id=tenant_id,
    )


def _resource(session, suffix: str, *, tenant_id: str = DEFAULT_TENANT_ID):
    stamp = now_utc()
    row = ResourceRow(
        id=f"v2e-resource-{suffix}-{tenant_id[-4:]}",
        tenant_id=tenant_id,
        resource_type="PROPERTY",
        canonical_name=f"Property {suffix}",
        status="ACTIVE",
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _event(
    session,
    *,
    tenant_id: str,
    suffix: str,
    resource_id: str,
    occurred_at=None,
    visibility: str = "PRIVATE",
):
    row = TimelineEventRow(
        id=f"v2e-event-{suffix}-{tenant_id[-4:]}",
        tenant_id=tenant_id,
        canonical_event_id=None,
        actor_id=f"actor-{suffix}",
        relationship_id=None,
        resource_id=resource_id,
        event_type="PROPERTY_ACTIVITY",
        event_ref={"source": "test"},
        occurred_at=occurred_at or now_utc(),
        visibility=visibility,
        provenance="TEST",
        metadata_json={},
    )
    session.add(row)
    session.flush()
    return row


def _episode(session, suffix: str = "1", *, visibility: str = "PRIVATE"):
    stamp = now_utc()
    resource = _resource(session, suffix)
    first = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix=f"{suffix}-a",
        resource_id=resource.id,
        occurred_at=stamp,
        visibility=visibility,
    )
    second = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix=f"{suffix}-b",
        resource_id=resource.id,
        occurred_at=stamp + timedelta(days=2),
        visibility=visibility,
    )
    result = assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=first.id,
        episode_type="PROPERTY_MATTER",
        scope_type="RESOURCE",
    )
    assign_timeline_event_to_episode(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        event_id=second.id,
        episode_type="PROPERTY_MATTER",
        scope_type="RESOURCE",
    )
    return resource, result.episode, first, second


def test_v2e_episode_consolidation_is_deterministic_and_idempotent(session):
    resource, episode, first, second = _episode(session, "deterministic")

    candidate, created = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )
    replay, replay_created = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )

    assert created is True
    assert replay_created is False
    assert replay.id == candidate.id
    assert candidate.state == "PROPOSED"
    assert candidate.source_engine == "RULE"
    assert candidate.insight_type == "CLAIM_PROPOSAL"
    assert candidate.subject_type == "RESOURCE"
    assert candidate.subject_id == resource.id
    assert candidate.predicate == "context.semantic_episode"
    assert candidate.proposed_value["episode_id"] == episode.id
    assert candidate.provenance["governance"] == "CANDIDATE_ONLY"
    assert candidate.provenance["grants_authority"] is False
    assert candidate.provenance["materializes_canonical_truth"] is False

    evidence = session.scalars(
        select(CandidateInsightEvidenceRow)
        .where(CandidateInsightEvidenceRow.candidate_id == candidate.id)
        .order_by(CandidateInsightEvidenceRow.evidence_type)
    ).all()
    assert len(evidence) == 3
    assert {row.source_ref for row in evidence} == {
        episode.id,
        first.id,
        second.id,
    }
    assert all(row.evidence_role == "SUPPORT" for row in evidence)


def test_v2e_learned_output_is_review_only_even_at_high_confidence(session):
    resource, episode, _, _ = _episode(session, "learned")

    candidate, created = propose_candidate_insight(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        insight_type="CLAIM_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.possible_recurring_matter",
        proposed_value={"kind": "RENT"},
        source_engine="LLM",
        confidence=0.999,
        evidence=[
            CandidateEvidenceInput(
                evidence_type="SEMANTIC_EPISODE",
                source_ref=episode.id,
                independence_key=f"episode:{episode.id}",
                confidence=1.0,
            )
        ],
    )

    assert created is True
    assert candidate.state == "NEEDS_REVIEW"
    assert candidate.confidence == pytest.approx(0.999)
    assert session.scalar(select(func.count()).select_from(FactRow)) == 0
    assert session.scalar(select(func.count()).select_from(EntityStateRow)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0




def test_v2e_gnn_engine_is_gated_until_v2j(session):
    resource, episode, _, _ = _episode(session, "gnn-gated")

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_ENGINE_UNSUPPORTED",
    ):
        propose_candidate_insight(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            insight_type="CLAIM_PROPOSAL",
            subject_type="RESOURCE",
            subject_id=resource.id,
            predicate="context.future_graph_prediction",
            proposed_value={"value": True},
            source_engine="GNN",
            confidence=0.99,
            evidence=[
                CandidateEvidenceInput(
                    evidence_type="SEMANTIC_EPISODE",
                    source_ref=episode.id,
                    independence_key=f"episode:{episode.id}",
                    confidence=1.0,
                )
            ],
        )

def test_v2e_owner_admission_does_not_materialize_canonical_truth(session):
    _owner(session)
    _, episode, _, _ = _episode(session, "admit")
    candidate, _ = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )

    admitted = admit_candidate_insight(
        session,
        candidate_id=candidate.id,
        decision_actor_key=OWNER,
        decision_ref="owner-command:v2e-admit-001",
    )

    assert admitted.state == "ADMITTED"
    assert admitted.decision_kind == "OWNER_ADMITTED"
    assert session.scalar(select(func.count()).select_from(FactRow)) == 0
    assert session.scalar(select(func.count()).select_from(EntityStateRow)) == 0
    assert session.scalar(select(func.count()).select_from(ExecutionIntentRow)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 0


def test_v2e_non_owner_cannot_admit_or_reject(session):
    _owner(session)
    upsert_actor_binding(
        session,
        source="whatsapp",
        external_actor_id="contact-v2e",
        actor_key="actor-contact-v2e",
        actor_category="contact",
        display_name="Contact",
        metadata={},
        tenant_id=DEFAULT_TENANT_ID,
    )
    _, episode, _, _ = _episode(session, "authority")
    candidate, _ = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_OWNER_AUTHORITY_REQUIRED",
    ):
        admit_candidate_insight(
            session,
            candidate_id=candidate.id,
            decision_actor_key="actor-contact-v2e",
            decision_ref="not-owner",
        )

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_OWNER_AUTHORITY_REQUIRED",
    ):
        reject_candidate_insight(
            session,
            candidate_id=candidate.id,
            decision_actor_key="actor-contact-v2e",
            decision_ref="not-owner",
        )


def test_v2e_contradiction_and_secret_evidence_fail_closed(session):
    _owner(session)
    resource = _resource(session, "contradiction")
    support = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="support",
        resource_id=resource.id,
        visibility="PRIVATE",
    )
    contradiction = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="contradiction",
        resource_id=resource.id,
        visibility="SECRET",
    )

    candidate, _ = propose_candidate_insight(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        insight_type="STATE_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.property_status",
        proposed_value={"status": "OCCUPIED"},
        source_engine="RULE",
        confidence=0.8,
        evidence=[
            CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT",
                source_ref=support.id,
                independence_key=f"event:{support.id}",
                confidence=0.9,
            ),
            CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT",
                source_ref=contradiction.id,
                independence_key=f"event:{contradiction.id}",
                confidence=0.9,
                evidence_role="CONTRADICTION",
            ),
        ],
    )

    assert candidate.state == "NEEDS_REVIEW"
    assert candidate.sensitivity_class == "SECRET"
    assert candidate.contradiction_refs == [contradiction.id]

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_CONTRADICTION_REQUIRES_REVIEW",
    ):
        admit_candidate_insight(
            session,
            candidate_id=candidate.id,
            decision_actor_key=OWNER,
            decision_ref="owner-command:v2e-admit-conflicted",
        )


def test_v2e_cross_tenant_evidence_fails_closed(session):
    tenant_b = "00000000-0000-4000-8000-00000000e224"
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_b,
            slug="v2e-tenant-b",
            name="V2E Tenant B",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()

    resource_a = _resource(session, "tenant-a")
    resource_b = _resource(session, "tenant-b", tenant_id=tenant_b)
    foreign_event = _event(
        session,
        tenant_id=tenant_b,
        suffix="tenant-b",
        resource_id=resource_b.id,
    )

    with pytest.raises(
        CandidateInsightError,
        match="CANDIDATE_INSIGHT_TENANT_MISMATCH",
    ):
        propose_candidate_insight(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            insight_type="CLAIM_PROPOSAL",
            subject_type="RESOURCE",
            subject_id=resource_a.id,
            predicate="context.foreign_evidence",
            proposed_value={"value": True},
            source_engine="RULE",
            confidence=1.0,
            evidence=[
                CandidateEvidenceInput(
                    evidence_type="TIMELINE_EVENT",
                    source_ref=foreign_event.id,
                    independence_key=f"event:{foreign_event.id}",
                    confidence=1.0,
                )
            ],
        )


def test_v2e_owner_rejection_is_durable_on_idempotent_replay(session):
    _owner(session)
    _, episode, _, _ = _episode(session, "reject")
    candidate, _ = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )

    rejected = reject_candidate_insight(
        session,
        candidate_id=candidate.id,
        decision_actor_key=OWNER,
        decision_ref="owner-command:v2e-reject-001",
    )
    replay, created = consolidate_episode_scope(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        episode_id=episode.id,
    )

    assert rejected.state == "REJECTED"
    assert created is False
    assert replay.id == candidate.id
    assert replay.state == "REJECTED"


def test_v2e_supersession_preserves_history_and_secret_sensitivity(session):
    _owner(session)
    resource = _resource(session, "supersede")
    secret = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="secret-old",
        resource_id=resource.id,
        visibility="SECRET",
    )
    private = _event(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        suffix="private-new",
        resource_id=resource.id,
        visibility="PRIVATE",
    )

    previous, _ = propose_candidate_insight(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        insight_type="CLAIM_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.property_role",
        proposed_value={"role": "OLD"},
        source_engine="RULE",
        confidence=0.7,
        evidence=[
            CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT",
                source_ref=secret.id,
                independence_key=f"event:{secret.id}",
                confidence=0.7,
            )
        ],
    )
    replacement, _ = propose_candidate_insight(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        insight_type="CLAIM_PROPOSAL",
        subject_type="RESOURCE",
        subject_id=resource.id,
        predicate="context.property_role",
        proposed_value={"role": "CORRECTED"},
        source_engine="RULE",
        confidence=1.0,
        evidence=[
            CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT",
                source_ref=private.id,
                independence_key=f"event:{private.id}",
                confidence=1.0,
            )
        ],
    )

    superseded = supersede_candidate_insight(
        session,
        candidate_id=previous.id,
        replacement_id=replacement.id,
        decision_actor_key=OWNER,
        decision_ref="owner-correction:v2e-001",
    )

    assert superseded.state == "SUPERSEDED"
    assert replacement.supersedes_insight_id == previous.id
    assert replacement.sensitivity_class == "SECRET"
    assert session.scalar(
        select(func.count()).select_from(CandidateInsightRow)
    ) == 2
