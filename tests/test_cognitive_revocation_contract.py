"""Acceptance gate: removing a source invalidates active context, not its history.

This currently reproduces the revocation gap in the isolated observation
candidate. Do not mark it xfail, weaken assertions, or use production data.
"""

from sqlalchemy import select

from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.models import TimelineEventRow
from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeRow
from test_cognitive_observation import _exercise


def test_revoked_artifact_withdraws_derived_context(session, monkeypatch):
    # Existing fixture creates three legitimate observations, one episode and
    # one candidate, then marks the common artifact as deleted. Re-observation
    # rejects the source but currently retains old derived state.
    _exercise(session, monkeypatch)

    active_episodes = session.scalars(
        select(SemanticEpisodeRow).where(SemanticEpisodeRow.state == "ACTIVE",
            SemanticEpisodeRow.evidence_status == "CURRENT")
    ).all()
    live_candidates = session.scalars(
        select(CandidateInsightRow).where(
            CandidateInsightRow.evidence_status == "CURRENT",
            CandidateInsightRow.state.in_(("PROPOSED", "NEEDS_REVIEW", "ADMITTED"))
        )
    ).all()
    historical_events = session.scalars(select(TimelineEventRow).where(
        TimelineEventRow.event_type == "ATTACHMENT_OBSERVED"
    )).all()

    assert len(historical_events) == 3, "Historical evidence must be preserved"
    assert not active_episodes and not live_candidates, (
        "Revoked source is still supporting current context: "
        f"active_episodes={len(active_episodes)}, "
        f"live_candidates={len(live_candidates)}"
    )


def test_readers_withdraw_revoked_source_before_reconciliation(session, monkeypatch):
    """Read-time guards must not wait for the next observation batch."""
    import pytest

    from attention_router.application.candidate_insights import (
        CandidateInsightError, admit_candidate_insight,
        candidate_insight_source_is_current,
    )
    from attention_router.application.cognitive_graph import build_cognitive_graph_slice
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    from datetime import timedelta
    from test_cognitive_observation import START
    evaluation_time = START + timedelta(days=3, hours=1)

    def revoke_and_check(db, observe, ids, registered):
        candidate = db.scalar(select(CandidateInsightRow))
        assert candidate.state == "PROPOSED"
        assert candidate_insight_source_is_current(db, candidate, now=evaluation_time)
        registered.artifact.status = "DELETED"

        assert not candidate_insight_source_is_current(db, candidate, now=evaluation_time)
        with pytest.raises(CandidateInsightError, match="EVIDENCE_REVOKED"):
            admit_candidate_insight(
                db, candidate_id=candidate.id,
                decision_actor_key="fixture-owner", decision_ref="synthetic:consent",
                now=evaluation_time,
            )
        graph = build_cognitive_graph_slice(
            db, tenant_id=DEFAULT_TENANT_ID, timeline_lineage="ORGANIC",
            now=evaluation_time,
        )
        assert not any(
            node.source_type in {"SEMANTIC_EPISODE", "TIMELINE_EVENT"}
            and (
                node.source_type == "SEMANTIC_EPISODE"
                or node.attributes.get("event_type") == "ATTACHMENT_OBSERVED"
            )
            for node in graph.nodes
        )

        withheld = observe(*ids)
        assert withheld.linked == withheld.candidates == 0
        assert db.scalar(select(SemanticEpisodeRow)).evidence_status == "EVIDENCE_REVOKED"
        assert candidate.evidence_status == "EVIDENCE_REVOKED"
        assert candidate.state == "PROPOSED"
        again = observe(*ids)
        assert again.new_links == again.new_candidates == again.new_memberships == 0

    _exercise(session, monkeypatch, prior_to_final_withdrawal=revoke_and_check)


def test_partial_revocation_preserves_remaining_support(session, monkeypatch):
    """One revoked message changes the evidence fingerprint, not the whole resource."""
    from datetime import timedelta

    from attention_router.application.candidate_insights import candidate_insight_source_is_current
    from attention_router.application.cognitive_graph import build_cognitive_graph_slice
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    from attention_router.infrastructure.models import ConversationMessageRow
    from test_cognitive_observation import START

    def revoke_one(db, observe, ids, registered):
        candidate = db.scalar(select(CandidateInsightRow))
        assert candidate.state == "PROPOSED"
        evaluation_time = START + timedelta(days=3, hours=1)
        assert candidate_insight_source_is_current(db, candidate, now=evaluation_time)
        event = db.scalar(select(TimelineEventRow).where(
            TimelineEventRow.event_type == "ATTACHMENT_OBSERVED",
            TimelineEventRow.event_ref["conversation_message_id"].as_string() == ids[0],
        ))
        assert event is not None
        db.get(ConversationMessageRow, ids[0]).searchable = False
        assert not candidate_insight_source_is_current(db, candidate, now=evaluation_time)
        result = observe(*ids)
        assert result.linked == 2
        assert result.new_candidates == 1
        assert candidate.evidence_status == "EVIDENCE_REVOKED"
        assert candidate.state == "PROPOSED"
        rows = db.scalars(select(CandidateInsightRow)).all()
        assert len(rows) == 2
        assert len([row for row in rows if row.state == "PROPOSED" and row.evidence_status == "CURRENT"]) == 1
        revised = next(row for row in rows if row.evidence_status == "CURRENT")
        assert revised.provenance["technical_revision_of"] == candidate.id
        assert revised.decision_ref is None

        episode = db.scalar(select(SemanticEpisodeRow))
        assert episode.state == "ACTIVE"
        assert episode.started_at.replace(tzinfo=START.tzinfo) == START + timedelta(days=2)
        assert episode.last_activity_at.replace(tzinfo=START.tzinfo) == START + timedelta(days=3)
        assert episode.provenance["technical_evidence_reconciliation"]["invalid_member_refs"]

        graph = build_cognitive_graph_slice(db, tenant_id=DEFAULT_TENANT_ID, timeline_lineage="ORGANIC", now=evaluation_time)
        assert not any(node.node_id == f"event:{event.id}" for node in graph.nodes)
        assert any(node.node_id == f"episode:{episode.id}" for node in graph.nodes)
        replay = observe(*ids)
        assert replay.new_links == replay.new_memberships == replay.new_candidates == 0

    _exercise(session, monkeypatch, prior_to_final_withdrawal=revoke_one)


def test_admission_history_survives_technical_revocation(session, monkeypatch):
    from datetime import timedelta
    from attention_router.application.candidate_insights import admit_candidate_insight
    from test_cognitive_observation import START

    def admit_then_revoke(db, observe, ids, registered):
        candidate = db.scalar(select(CandidateInsightRow))
        admit_candidate_insight(
            db, candidate_id=candidate.id, decision_actor_key="fixture-owner",
            decision_ref="fixture-owner-decision", now=START + timedelta(days=3, hours=1),
        )
        decision = (candidate.state, candidate.decision_kind, candidate.decision_actor_key,
                    candidate.decision_ref, candidate.decided_at)
        registered.artifact.status = "DELETED"
        observe(*ids)
        assert candidate.evidence_status == "EVIDENCE_REVOKED"
        assert decision == (candidate.state, candidate.decision_kind, candidate.decision_actor_key,
                            candidate.decision_ref, candidate.decided_at)
        registered.artifact.status = "AVAILABLE"
        replay = observe(*ids)
        assert replay.linked == replay.new_candidates == 0
        assert candidate.evidence_status == "EVIDENCE_REVOKED"

    _exercise(session, monkeypatch, prior_to_final_withdrawal=admit_then_revoke)


def test_stale_relation_graph_cannot_persist_proposal_with_withdrawn_support(session, monkeypatch):
    from datetime import timedelta

    import pytest

    from attention_router.application.candidate_insights import CandidateInsightError
    from attention_router.application.cognitive_graph import build_cognitive_graph_slice
    from attention_router.application.graph_intelligence import (
        DeterministicRelationCandidateEngine,
        persist_relation_candidate_insight,
    )
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    from attention_router.domain.graph_intelligence import GraphIntelligenceBudget
    from test_cognitive_observation import START

    stamp = START + timedelta(days=3, hours=1)

    def propose_after_revocation(db, observe, ids, registered):
        graph = build_cognitive_graph_slice(
            db, DEFAULT_TENANT_ID, timeline_lineage="ORGANIC", now=stamp,
        )
        candidates = DeterministicRelationCandidateEngine().candidates(
            graph, budget=GraphIntelligenceBudget(),
        )
        assert candidates
        candidate = candidates[0]
        assert len(candidate.support_event_ids) == 3
        registered.artifact.status = "DELETED"
        with pytest.raises(CandidateInsightError, match="EVIDENCE_REVOKED"):
            persist_relation_candidate_insight(db, graph=graph, candidate=candidate, now=stamp)

    _exercise(session, monkeypatch, prior_to_final_withdrawal=propose_after_revocation)


def test_reconciliation_cannot_tombstone_another_accounts_event(session, monkeypatch):
    from datetime import timedelta

    from attention_router.application.cognitive_observation import (
        observation_event_is_current,
        observe_attachment_context,
    )
    from attention_router.application.semantic_episodes import assign_timeline_event_to_episode
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    from test_cognitive_observation import START, _attachment

    stamp = START + timedelta(days=3, hours=1)

    def reconcile_after_bad_membership(db, observe, ids, registered):
        original_episode = db.scalar(select(SemanticEpisodeRow))
        other_account = "other-fixture-account"
        message_id, _, _ = _attachment(db, "separate-receipt", 3, account=other_account)
        result = observe_attachment_context(
            db, tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
            owner_human_identity_id="fixture-human-owner", source_account=other_account,
            message_ids=(message_id,), start=START, now=stamp,
        )
        assert result.new_links == 1
        other_event = db.scalar(select(TimelineEventRow).where(
            TimelineEventRow.event_ref["conversation_message_id"].as_string() == message_id,
        ))
        assert observation_event_is_current(db, other_event, source_account=other_account, now=stamp)
        assignment = assign_timeline_event_to_episode(
            db, tenant_id=DEFAULT_TENANT_ID, event_id=other_event.id,
            episode_type=original_episode.episode_type, scope_type="RESOURCE", now=stamp,
        )
        assert assignment.episode.id == original_episode.id

        observe(*ids)
        assert not other_event.metadata_json.get("technical_evidence_revoked")
        assert observation_event_is_current(db, other_event, source_account=other_account, now=stamp)

    _exercise(session, monkeypatch, prior_to_final_withdrawal=reconcile_after_bad_membership)


def test_candidate_predicate_cannot_hide_withdrawn_timeline_support(session, monkeypatch):
    from datetime import timedelta

    from attention_router.application.candidate_insights import (
        CandidateEvidenceInput,
        candidate_insight_source_is_current,
        propose_candidate_insight,
    )
    from attention_router.core.tenancy import DEFAULT_TENANT_ID
    from test_cognitive_observation import START

    stamp = START + timedelta(days=3, hours=1)

    def check_proposal(db, observe, ids, registered):
        event = db.scalars(select(TimelineEventRow).where(
            TimelineEventRow.event_type == "ATTACHMENT_OBSERVED",
        )).first()
        candidate, created = propose_candidate_insight(
            db, tenant_id=DEFAULT_TENANT_ID,
            insight_type="CLAIM_PROPOSAL", subject_type="RESOURCE", subject_id=event.resource_id,
            predicate="context.recurring_obligation", proposed_value={"fixture": True},
            source_engine="RULE", confidence=0.8, now=stamp,
            evidence=[CandidateEvidenceInput(
                evidence_type="TIMELINE_EVENT", source_ref=event.id,
                independence_key=event.id, confidence=0.8,
            )],
        )
        assert created and candidate_insight_source_is_current(db, candidate, now=stamp)
        registered.artifact.status = "DELETED"
        assert not candidate_insight_source_is_current(db, candidate, now=stamp)

    _exercise(session, monkeypatch, prior_to_final_withdrawal=check_proposal)
