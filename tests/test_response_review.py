from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from attention_router.application import response_review
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
)


def _decision(session, decision_id="decision-review-test"):
    now = datetime.now(timezone.utc)
    session.add(InboundEventRow(
        id="event-review-test", source="test", external_event_id="external-review-test",
        event_type="message", payload={"safe": True}, payload_hash="hash",
        received_at=now, interaction_id="interaction-review-test", status="processed",
        correlation_id="correlation-review-test",
    ))
    session.add(InteractionRow(
        id="interaction-review-test", event_type="message", contact_id="masked",
        contact_name="Test", relationship_category="unknown", inbound_text="test",
        state="active", policy_id=None, policy_version_id=None, correlation_id="correlation-review-test",
        causation_id=None, lia_speech=None, created_at=now, updated_at=now,
    ))
    row = AgentDecisionRow(
        id=decision_id, event_id="event-review-test", interaction_id="interaction-review-test",
        agent_blueprint_id=None, agent_blueprint_version=None, actor_id=None, actor_binding_id=None,
        audience="unknown", policy_version_id=None, decision_pipeline_version="v1",
        decision_type="RESPOND", recommended_action="soft_ping", proposed_response="Resposta proposta.",
        escalation_required=False, escalation_reason=None, confidence=0.8, missing_information=[],
        execution_allowed=False, external_delivery_allowed=False, reasoning_summary="safe", status="DRY_RUN",
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def test_review_creation_edit_approve_is_idempotent_and_blocked(session):
    decision = _decision(session)
    review = response_review.create_review_for_decision(session, decision.id)
    assert response_review.create_review_for_decision(session, decision.id).id == review.id
    response_review.edit_review(session, review.id, "Resposta editada.")
    approved, intent = response_review.approve_review(session, review.id)
    _, duplicate_intent = response_review.approve_review(session, review.id)
    session.commit()
    assert approved.status == "APPROVED"
    assert intent.id == duplicate_intent.id
    assert intent.status == "BLOCKED"
    assert intent.effective_response_snapshot == "Resposta editada."
    assert intent.external_delivery_allowed is False
    assert intent.blocked_reason == "EXTERNAL_DELIVERY_DISABLED"
    assert session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.response_review_id == review.id)) is not None
    assert session.scalar(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")) is None


def test_reject_creates_no_execution_intent(session):
    decision = _decision(session, "decision-reject-test")
    review = response_review.create_review_for_decision(session, decision.id)
    rejected = response_review.reject_review(session, review.id, "requires clarification")
    session.commit()
    assert rejected.status == "REJECTED"
    assert session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.response_review_id == review.id)) is None


def test_terminal_transitions_are_rejected(session):
    decision = _decision(session, "decision-transition-test")
    review = response_review.create_review_for_decision(session, decision.id)
    response_review.reject_review(session, review.id)
    with pytest.raises(response_review.ReviewConflict):
        response_review.approve_review(session, review.id)


def test_approved_review_is_terminal(session):
    decision = _decision(session, "decision-approved-terminal-test")
    review = response_review.create_review_for_decision(session, decision.id)
    response_review.approve_review(session, review.id)
    with pytest.raises(response_review.ReviewConflict):
        response_review.reject_review(session, review.id)
    with pytest.raises(response_review.ReviewConflict):
        response_review.edit_review(session, review.id, "alteracao proibida")
