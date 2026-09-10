from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.application.platform.context import resolve_represented_subject
from attention_router.application.response_review import (
    ReviewConflict,
    ReviewNotFound,
    approve_review,
    reject_review,
)
from attention_router.core.events import OperatorAuthority
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentDecisionRow,
    AgentResponseReviewRow,
    InteractionRow,
    OutboxMessageRow,
)
from attention_router.infrastructure.repository import audit


OWNER_REVIEW_REFERENCE_LENGTH = 12
OWNER_REVIEW_BINDING_SOURCE = "wwebjs"
OWNER_REVIEW_REQUEST_ACTION = "owner_response_review_request_text"
OWNER_REVIEW_REJECTION_REASON = "OWNER_REJECTED_VIA_SELF_CHAT"


class OwnerResponseReviewError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerResponseReviewMutation:
    review_id: str
    review_status: str
    execution_intent_id: str | None
    changed: bool
    duplicate: bool


def review_reference(review_id: str) -> str:
    value = review_id.strip().casefold()
    if len(value) < OWNER_REVIEW_REFERENCE_LENGTH:
        raise OwnerResponseReviewError("OWNER_REVIEW_REFERENCE_INVALID")
    return value[:OWNER_REVIEW_REFERENCE_LENGTH]


def _review_for_reference(
    session: Session,
    *,
    tenant_id: str,
    reference: str,
) -> AgentResponseReviewRow:
    normalized = reference.strip().casefold()
    if not normalized or len(normalized) < 8:
        raise OwnerResponseReviewError("OWNER_REVIEW_REFERENCE_INVALID")
    matches = session.scalars(
        select(AgentResponseReviewRow)
        .join(AgentDecisionRow, AgentDecisionRow.id == AgentResponseReviewRow.agent_decision_id)
        .join(InteractionRow, InteractionRow.id == AgentDecisionRow.interaction_id)
        .where(
            InteractionRow.tenant_id == tenant_id,
            func.lower(AgentResponseReviewRow.id).like(f"{normalized}%"),
        )
        .order_by(AgentResponseReviewRow.created_at.desc())
        .limit(2)
    ).all()
    if not matches:
        raise OwnerResponseReviewError("OWNER_REVIEW_NOT_FOUND")
    if len(matches) != 1:
        raise OwnerResponseReviewError("OWNER_REVIEW_REFERENCE_AMBIGUOUS")
    return matches[0]


def apply_owner_response_review(
    session: Session,
    *,
    tenant_id: str,
    reference: str,
    approve: bool,
    authority: OperatorAuthority,
) -> OwnerResponseReviewMutation:
    if (
        not authority.authenticated
        or authority.tenant_id != tenant_id
        or "OWNER" not in authority.roles
    ):
        raise OwnerResponseReviewError("OWNER_AUTHORITY_UNAVAILABLE")

    row = _review_for_reference(session, tenant_id=tenant_id, reference=reference)
    previous_status = row.status
    reviewer_reference = f"owner:{stable_hash(authority.operator_actor_id)[:16]}"
    try:
        if approve:
            row, intent = approve_review(
                session,
                row.id,
                reviewer_reference=reviewer_reference,
            )
            execution_intent_id = intent.id
            expected_terminal = "APPROVED"
        else:
            row = reject_review(
                session,
                row.id,
                reason=OWNER_REVIEW_REJECTION_REASON,
                reviewer_reference=reviewer_reference,
            )
            execution_intent_id = None
            expected_terminal = "REJECTED"
    except (ReviewNotFound, ReviewConflict) as exc:
        raise OwnerResponseReviewError(str(exc)) from exc

    duplicate = previous_status == expected_terminal
    return OwnerResponseReviewMutation(
        review_id=row.id,
        review_status=row.status,
        execution_intent_id=execution_intent_id,
        changed=not duplicate,
        duplicate=duplicate,
    )


def _owner_binding_for_tenant(session: Session, tenant_id: str) -> ActorBindingRow | None:
    represented = resolve_represented_subject(session, tenant_id)
    if represented is None:
        return None
    matches = session.scalars(
        select(ActorBindingRow)
        .where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == represented.entity_id,
            ActorBindingRow.source == OWNER_REVIEW_BINDING_SOURCE,
            ActorBindingRow.is_active.is_(True),
        )
        .order_by(ActorBindingRow.created_at.desc())
        .limit(2)
    ).all()
    return matches[0] if len(matches) == 1 else None


def render_owner_response_review_request(review: AgentResponseReviewRow) -> str:
    reference = review_reference(review.id)
    response = review.effective_response.strip()
    if len(response) > 700:
        response = response[:697].rstrip() + "..."
    return (
        "Andy quer sua aprovação.\n\n"
        f"Resposta proposta:\n{response}\n\n"
        f"Referência: {reference}\n"
        f"Para aprovar: aprovar resposta {reference}\n"
        f"Para negar: negar resposta {reference}"
    )


def enqueue_owner_response_review_request(
    session: Session,
    review: AgentResponseReviewRow,
) -> OutboxMessageRow | None:
    if review.status != "PENDING":
        return None
    decision = session.get(AgentDecisionRow, review.agent_decision_id)
    if decision is None:
        raise OwnerResponseReviewError("OWNER_REVIEW_DECISION_NOT_FOUND")
    interaction = session.get(InteractionRow, decision.interaction_id)
    if interaction is None:
        raise OwnerResponseReviewError("OWNER_REVIEW_INTERACTION_NOT_FOUND")

    idempotency_key = f"owner-response-review:request:{review.id}"
    existing = session.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    binding = _owner_binding_for_tenant(session, interaction.tenant_id)
    if binding is None:
        audit(
            session,
            interaction.id,
            "response_review.owner_request_blocked",
            {"review_id": review.id, "reason": "OWNER_WWEBJS_BINDING_UNAVAILABLE_OR_AMBIGUOUS"},
            origin="owner_response_review",
        )
        return None

    stamp = now_utc()
    outbox = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction.id,
        action_type=OWNER_REVIEW_REQUEST_ACTION,
        destination="local_transport",
        payload={
            "external_actor_id": binding.external_actor_id,
            "message_type": "text",
            "text": render_owner_response_review_request(review),
        },
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=idempotency_key,
        correlation_id=interaction.correlation_id,
        causation_id=review.id,
        execution_intent_id=None,
    )
    session.add(outbox)
    audit(
        session,
        interaction.id,
        "response_review.owner_request_enqueued",
        {"review_id": review.id, "outbox_id": outbox.id},
        interaction.correlation_id,
        review.id,
        origin="owner_response_review",
    )
    return outbox


__all__ = [
    "OWNER_REVIEW_REFERENCE_LENGTH",
    "OwnerResponseReviewError",
    "OwnerResponseReviewMutation",
    "apply_owner_response_review",
    "enqueue_owner_response_review_request",
    "render_owner_response_review_request",
    "review_reference",
]
