from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.execution_safety import (
    reserve_synthetic_system_effect_for_intent_in_transaction,
)


class ReviewError(ValueError):
    pass


class ReviewNotFound(ReviewError):
    pass


class ReviewConflict(ReviewError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_review_for_decision(session: Session, decision_id: str, reviewer_type: str = "operator") -> AgentResponseReviewRow:
    decision = session.get(AgentDecisionRow, decision_id)
    if decision is None:
        raise ReviewNotFound("agent decision not found")
    if not decision.proposed_response:
        raise ReviewError("agent decision has no proposed response")
    existing = session.scalar(select(AgentResponseReviewRow).where(AgentResponseReviewRow.agent_decision_id == decision_id))
    if existing:
        return existing
    now = _now()
    row = AgentResponseReviewRow(
        id=new_id(), agent_decision_id=decision_id, status="PENDING",
        proposed_response_snapshot=decision.proposed_response, effective_response=decision.proposed_response,
        reviewer_type=reviewer_type, version=1, created_at=now, updated_at=now,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return session.scalar(select(AgentResponseReviewRow).where(AgentResponseReviewRow.agent_decision_id == decision_id))
    audit(session, decision.interaction_id, "response_review.created", {"review_id": row.id, "decision_id": decision_id})
    return row


def _decision(session: Session, row: AgentResponseReviewRow) -> AgentDecisionRow:
    return session.get(AgentDecisionRow, row.agent_decision_id)


def review_to_dict(session: Session, row: AgentResponseReviewRow) -> dict[str, Any]:
    decision = _decision(session, row)
    return {
        "id": row.id, "agent_decision_id": row.agent_decision_id, "status": row.status,
        "proposed_response": row.proposed_response_snapshot, "edited_response": row.edited_response,
        "effective_response": row.effective_response, "version": row.version,
        "reviewer_type": row.reviewer_type, "review_reason": row.review_reason,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "decision_type": decision.decision_type, "recommended_action": decision.recommended_action,
        "audience": decision.audience, "policy_version_id": decision.policy_version_id,
        "confidence": decision.confidence, "escalation_required": decision.escalation_required,
        "escalation_reason": decision.escalation_reason,
    }


def list_reviews(session: Session, status: str | None = None, decision_id: str | None = None, interaction_id: str | None = None) -> list[AgentResponseReviewRow]:
    query = select(AgentResponseReviewRow).join(AgentDecisionRow).order_by(AgentResponseReviewRow.created_at)
    if status:
        query = query.where(AgentResponseReviewRow.status == status)
    if decision_id:
        query = query.where(AgentResponseReviewRow.agent_decision_id == decision_id)
    if interaction_id:
        query = query.where(AgentDecisionRow.interaction_id == interaction_id)
    return list(session.scalars(query.limit(100)).all())


def edit_review(session: Session, review_id: str, text: str, reviewer_reference: str | None = None) -> AgentResponseReviewRow:
    row = session.get(AgentResponseReviewRow, review_id)
    if row is None:
        raise ReviewNotFound("review not found")
    if row.status != "PENDING":
        raise ReviewConflict(f"review cannot be edited from {row.status}")
    text = text.strip()
    if not text:
        raise ReviewError("edited response cannot be empty")
    row.edited_response = text
    row.effective_response = text
    row.version += 1
    row.updated_at = _now()
    row.reviewer_reference = reviewer_reference or row.reviewer_reference
    decision = _decision(session, row)
    audit(session, decision.interaction_id, "response_review.edited", {"review_id": row.id, "version": row.version, "response_hash": stable_hash(text), "response_length": len(text)})
    return row


def _intent(session: Session, row: AgentResponseReviewRow) -> AgentExecutionIntentRow | None:
    return session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.response_review_id == row.id))


def approve_review(session: Session, review_id: str, reviewer_reference: str | None = None) -> tuple[AgentResponseReviewRow, AgentExecutionIntentRow]:
    row = session.scalar(select(AgentResponseReviewRow).where(AgentResponseReviewRow.id == review_id).with_for_update())
    if row is None:
        raise ReviewNotFound("review not found")
    if row.status == "APPROVED":
        intent = _intent(session, row)
        if intent is None:
            raise ReviewConflict("approved review has no execution intent")
        reserve_synthetic_system_effect_for_intent_in_transaction(
            session,
            intent=intent,
            readiness_max_age_seconds=settings.readiness_result_max_age,
        )
        return row, intent
    if row.status != "PENDING":
        raise ReviewConflict(f"review cannot be approved from {row.status}")
    row.status = "APPROVED"
    row.reviewed_at = _now()
    row.updated_at = row.reviewed_at
    row.reviewer_reference = reviewer_reference or row.reviewer_reference
    decision = _decision(session, row)
    audit(session, decision.interaction_id, "response_review.approved", {"review_id": row.id, "decision_id": decision.id})
    existing = _intent(session, row) or session.scalar(
        select(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.idempotency_key == f"response-review:{row.id}"
        )
    )
    if existing and existing.status == "CANCELLED":
        existing.response_review_id = row.id
        existing.authorization_source = "HUMAN_REVIEW"
        existing.intent_type = "WHATSAPP_RESPONSE"
        existing.effective_response_snapshot = row.effective_response
        existing.status = "BLOCKED"
        existing.execution_allowed = False
        existing.external_delivery_allowed = False
        existing.blocked_reason = "EXTERNAL_DELIVERY_DISABLED"
        existing.release_status = "HELD"
        existing.released_at = None
        existing.released_by = None
        existing.recipient_reference = None
        existing.autonomy_evaluation_id = None
        existing.capability_name = None
        existing.provider_instance_id = None
        existing.canonical_event_id = None
        session.flush()
        reserve_synthetic_system_effect_for_intent_in_transaction(
            session,
            intent=existing,
            readiness_max_age_seconds=settings.readiness_result_max_age,
        )
        audit(session, decision.interaction_id, "execution_intent.reused", {"intent_id": existing.id, "review_id": row.id})
        return row, existing
    intent = AgentExecutionIntentRow(
        id=new_id(), agent_decision_id=decision.id, response_review_id=row.id, authorization_source="HUMAN_REVIEW",
        intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot=row.effective_response, status="BLOCKED", execution_allowed=False,
        external_delivery_allowed=False, blocked_reason="EXTERNAL_DELIVERY_DISABLED",
        idempotency_key=f"response-review:{row.id}", created_at=_now(),
    )
    session.add(intent)
    session.flush()
    reserve_synthetic_system_effect_for_intent_in_transaction(
        session,
        intent=intent,
        readiness_max_age_seconds=settings.readiness_result_max_age,
    )
    audit(session, decision.interaction_id, "execution_intent.created", {"intent_id": intent.id, "review_id": row.id})
    audit(session, decision.interaction_id, "execution_intent.blocked", {"intent_id": intent.id, "reason": intent.blocked_reason})
    return row, intent


def reject_review(session: Session, review_id: str, reason: str | None = None, reviewer_reference: str | None = None) -> AgentResponseReviewRow:
    row = session.get(AgentResponseReviewRow, review_id)
    if row is None:
        raise ReviewNotFound("review not found")
    if row.status == "REJECTED":
        return row
    if row.status != "PENDING":
        raise ReviewConflict(f"review cannot be rejected from {row.status}")
    row.status = "REJECTED"
    row.review_reason = reason
    row.reviewed_at = _now()
    row.updated_at = row.reviewed_at
    row.reviewer_reference = reviewer_reference or row.reviewer_reference
    decision = _decision(session, row)
    audit(session, decision.interaction_id, "response_review.rejected", {"review_id": row.id, "decision_id": decision.id, "has_reason": bool(reason)})
    return row


def intent_to_dict(row: AgentExecutionIntentRow) -> dict[str, Any]:
    return {"id": row.id, "agent_decision_id": row.agent_decision_id, "response_review_id": row.response_review_id, "intent_type": row.intent_type, "status": row.status, "execution_allowed": row.execution_allowed, "external_delivery_allowed": row.external_delivery_allowed, "blocked_reason": row.blocked_reason, "idempotency_key": row.idempotency_key, "created_at": row.created_at.isoformat()}
