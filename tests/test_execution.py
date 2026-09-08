from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from attention_router.application import execution
from attention_router.config import settings
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
)


def _intent(session):
    now = datetime.now(timezone.utc)
    session.add(InteractionRow(
        id="interaction-execution-test", event_type="message", contact_id="masked",
        contact_name="Test", relationship_category="unknown", inbound_text="inbound",
        state="active", policy_id=None, policy_version_id=None, correlation_id="execution-correlation",
        causation_id=None, lia_speech=None, created_at=now, updated_at=now,
    ))
    session.flush()
    session.add(InboundEventRow(
        id="event-execution-test", source="wwebjs", external_event_id="external-execution-test",
        event_type="message", payload={"external_actor_id": "contact@c.us", "channel": "whatsapp"},
        payload_hash="execution-hash", received_at=now, interaction_id="interaction-execution-test",
        status="processed", correlation_id="execution-correlation",
    ))
    session.flush()
    session.add(AgentDecisionRow(
        id="decision-execution-test", event_id="event-execution-test", interaction_id="interaction-execution-test",
        decision_pipeline_version="v1", decision_type="RESPOND", recommended_action="reply",
        proposed_response="Resposta controlada.", escalation_required=False, confidence=1,
        missing_information=[], execution_allowed=False, external_delivery_allowed=False,
        reasoning_summary="test", status="DRY_RUN", created_at=now,
    ))
    review = AgentResponseReviewRow(
        id="review-execution-test", agent_decision_id="decision-execution-test", status="APPROVED",
        proposed_response_snapshot="Resposta controlada.", effective_response="Resposta controlada.",
        reviewer_type="operator", version=1, created_at=now, updated_at=now,
    )
    session.add(review)
    session.flush()
    row = AgentExecutionIntentRow(
        id="intent-execution-test", agent_decision_id="decision-execution-test",
        response_review_id=review.id, intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot=review.effective_response, status="BLOCKED",
        execution_allowed=False, external_delivery_allowed=False,
        blocked_reason="EXTERNAL_DELIVERY_DISABLED", idempotency_key="execution:test",
        created_at=now, release_status="HELD",
    )
    session.add(row)
    session.flush()
    return row


def test_global_gate_and_per_intent_release_are_independent(session, monkeypatch):
    intent = _intent(session)
    monkeypatch.setattr(settings, "external_delivery_enabled", False)
    with pytest.raises(execution.ExecutionBlocked, match="EXTERNAL_DELIVERY_DISABLED"):
        execution.release_intent(session, intent.id, transport_ready=True)
    assert intent.release_status == "HELD"


def test_released_intent_enqueues_once_and_sends_nothing_without_transport(session, monkeypatch):
    intent = _intent(session)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    execution.release_intent(session, intent.id, transport_ready=True)
    assert intent.status == "READY"
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    outboxes = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.execution_intent_id == intent.id)).all()
    assert len(outboxes) == 1
    assert outboxes[0].idempotency_key == "execution:intent-execution-test"


def test_platform_execution_safety_denies_outbox_without_reserved_budget(
    session,
    monkeypatch,
):
    intent = _intent(session)
    event = session.get(InboundEventRow, "event-execution-test")
    event.lineage_classification = "SYNTHETIC"
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    execution.release_intent(session, intent.id, transport_ready=True)
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    assert intent.status == "BLOCKED"
    assert intent.blocked_reason == "EFFECT_BUDGET_MISSING"
    assert session.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.execution_intent_id == intent.id)
    ) is None


def test_release_is_idempotent(session, monkeypatch):
    intent = _intent(session)
    monkeypatch.setattr(settings, "external_delivery_enabled", True)
    first = execution.release_intent(session, intent.id, transport_ready=True)
    second = execution.release_intent(session, intent.id, transport_ready=True)
    assert first.id == second.id
    assert second.status == "READY"
