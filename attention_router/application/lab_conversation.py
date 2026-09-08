from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    AgentDecisionRow, AgentExecutionIntentRow, AgentResponseReviewRow,
    InboundEventRow, LabConversationInboundRow, LabConversationSessionRow,
    LabDeliveryPermitRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.observability.tracing import current_span, safe_set_attribute, set_outcome, traced_span


def now():
    return datetime.now(timezone.utc)


def start_session(session: Session, binding: str, policy: str, max_inbounds: int = 12, max_sends: int = 8, created_by: str = "operator") -> LabConversationSessionRow:
    if session.scalar(select(LabConversationSessionRow).where(LabConversationSessionRow.target_binding_id == binding, LabConversationSessionRow.status == "ACTIVE")):
        raise ValueError("active lab session already exists for binding")
    row = LabConversationSessionRow(id=new_id(), target_binding_id=binding, target_policy=policy, status="ACTIVE", started_at=now(), max_inbounds=max_inbounds, max_send_permits=max_sends, inbound_count=0, send_permit_count=0, active_slot=binding, created_at=now(), created_by=created_by)
    session.add(row)
    session.flush()
    return row


def stop_session(session: Session, session_id: str) -> LabConversationSessionRow:
    row = session.get(LabConversationSessionRow, session_id)
    if not row:
        raise ValueError("lab session not found")
    row.status = "STOPPED"
    row.stopped_at = now()
    row.active_slot = None
    session.flush()
    return row


@traced_span("lab.session.claim_inbound")
def claim_inbound(session: Session, event: InboundEventRow, binding_id: str | None, policy_id: str | None) -> LabConversationInboundRow | None:
    if not binding_id:
        return None
    existing = session.scalar(select(LabConversationInboundRow).where(LabConversationInboundRow.inbound_event_id == event.id))
    if existing:
        set_outcome(current_span(), "ALREADY_CLAIMED")
        return existing
    row = session.scalar(select(LabConversationSessionRow).where(LabConversationSessionRow.target_binding_id == binding_id, LabConversationSessionRow.target_policy == policy_id, LabConversationSessionRow.status == "ACTIVE").with_for_update())
    if not row or row.inbound_count >= row.max_inbounds:
        set_outcome(current_span(), "NOT_ELIGIBLE")
        return None
    claim = LabConversationInboundRow(session_id=row.id, inbound_event_id=event.id, ordinal=row.inbound_count + 1, claimed_at=now())
    row.inbound_count += 1
    session.add(claim)
    safe_set_attribute(current_span(), "attention.lab_session_id", row.id)
    safe_set_attribute(current_span(), "attention.lab.inbound_ordinal", claim.ordinal)
    safe_set_attribute(current_span(), "attention.lab.budget_remaining", row.max_inbounds - row.inbound_count)
    set_outcome(current_span(), "CLAIMED")
    audit(session, event.interaction_id, "LAB_INBOUND_CLAIMED", {"session_id": row.id, "inbound_ordinal": claim.ordinal}, origin="lab_conversation")
    session.flush()
    return claim


@traced_span("lab.delivery.reserve")
def reserve_permit(session: Session, review: AgentResponseReviewRow) -> LabDeliveryPermitRow | None:
    existing = session.scalar(select(LabDeliveryPermitRow).where(LabDeliveryPermitRow.review_id == review.id))
    if existing:
        set_outcome(current_span(), "IDEMPOTENT")
        return existing
    decision = session.get(AgentDecisionRow, review.agent_decision_id)
    if not decision or decision.response_source != "OPENAI_AGENTS_SDK" or not decision.proposed_response:
        set_outcome(current_span(), "BLOCKED")
        return None
    inbound = session.get(InboundEventRow, decision.event_id)
    membership = session.scalar(select(LabConversationInboundRow).where(LabConversationInboundRow.inbound_event_id == inbound.id)) if inbound else None
    if not membership:
        set_outcome(current_span(), "BLOCKED")
        return None
    lab = session.scalar(select(LabConversationSessionRow).where(LabConversationSessionRow.id == membership.session_id).with_for_update())
    if not lab or lab.status != "ACTIVE" or lab.send_permit_count >= lab.max_send_permits:
        set_outcome(current_span(), "BLOCKED")
        return None
    permit = LabDeliveryPermitRow(id=new_id(), session_id=lab.id, review_id=review.id, target_binding_id=lab.target_binding_id, permit_ordinal=lab.send_permit_count + 1, status="RESERVED", created_at=now())
    lab.send_permit_count += 1
    session.add(permit)
    safe_set_attribute(current_span(), "attention.lab_session_id", lab.id)
    safe_set_attribute(current_span(), "attention.lab.send_permit_ordinal", permit.permit_ordinal)
    safe_set_attribute(current_span(), "attention.lab.budget_remaining", lab.max_send_permits - lab.send_permit_count)
    set_outcome(current_span(), "RESERVED")
    audit(session, decision.interaction_id, "LAB_DELIVERY_PERMIT_RESERVED", {"session_id": lab.id, "review_id": review.id, "permit_ordinal": permit.permit_ordinal}, origin="lab_conversation")
    session.flush()
    return permit


@traced_span("lab.delivery.validate")
def permit_for_intent(session: Session, intent: AgentExecutionIntentRow) -> LabDeliveryPermitRow | None:
    review = session.get(AgentResponseReviewRow, intent.response_review_id) if intent.response_review_id else None
    if not review:
        set_outcome(current_span(), "BLOCKED")
        return None
    permit = session.scalar(select(LabDeliveryPermitRow).where(LabDeliveryPermitRow.review_id == review.id).with_for_update())
    if not permit:
        set_outcome(current_span(), "BLOCKED")
        return None
    lab = session.get(LabConversationSessionRow, permit.session_id)
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    inbound = session.get(InboundEventRow, decision.event_id) if decision else None
    membership = session.scalar(select(LabConversationInboundRow).where(LabConversationInboundRow.inbound_event_id == inbound.id)) if inbound else None
    if not lab or lab.status != "ACTIVE" or not membership or membership.session_id != lab.id:
        set_outcome(current_span(), "BLOCKED")
        return None
    if permit.consumed_execution_intent_id and permit.consumed_execution_intent_id != intent.id:
        set_outcome(current_span(), "BLOCKED")
        return None
    if permit.status != "RESERVED" and permit.consumed_execution_intent_id != intent.id:
        set_outcome(current_span(), "BLOCKED")
        return None
    safe_set_attribute(current_span(), "attention.lab_session_id", lab.id)
    set_outcome(current_span(), "ALLOWED")
    return permit


@traced_span("lab.delivery.consume")
def consume_permit(session: Session, permit: LabDeliveryPermitRow, intent: AgentExecutionIntentRow) -> None:
    if permit.consumed_execution_intent_id == intent.id:
        set_outcome(current_span(), "IDEMPOTENT")
        return
    if permit.consumed_execution_intent_id:
        raise ValueError("lab permit belongs to another execution intent")
    permit.consumed_execution_intent_id = intent.id
    permit.consumed_at = now()
    permit.status = "CONSUMED"
    safe_set_attribute(current_span(), "attention.lab_session_id", permit.session_id)
    set_outcome(current_span(), "CONSUMED")
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    audit(session, decision.interaction_id, "LAB_DELIVERY_PERMIT_CONSUMED", {"permit_id": permit.id, "intent_id": intent.id}, origin="lab_conversation")
