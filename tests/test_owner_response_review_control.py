from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseStatus,
    parse_owner_grace_control,
)
from attention_router.application import response_review, services
from attention_router.application.owner_control import OwnerControlAction
from attention_router.application.owner_response_review_control import (
    OWNER_REVIEW_REQUEST_ACTION,
    OwnerResponseReviewError,
    apply_owner_response_review,
    review_reference,
)
from attention_router.core.events import OperatorAuthority
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
)


def _owner_binding(session, *, external_actor_id="synthetic-owner@c.us") -> ActorBindingRow:
    now = datetime.now(timezone.utc)
    row = ActorBindingRow(
        id="binding-owner-review",
        source="wwebjs",
        external_actor_id=external_actor_id,
        actor_key="owner-actor",
        display_name="Synthetic Owner",
        actor_category="owner",
        active_context="owner_control",
        is_active=True,
        binding_metadata={"owner": True},
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _decision(session, *, suffix="base") -> AgentDecisionRow:
    now = datetime.now(timezone.utc)
    event_id = f"event-owner-review-{suffix}"
    interaction_id = f"interaction-owner-review-{suffix}"
    decision_id = f"decision-owner-review-{suffix}"
    session.add(
        InteractionRow(
            id=interaction_id,
            event_type="message",
            contact_id=f"contact-{suffix}",
            contact_name="Synthetic Contact",
            relationship_category="unknown",
            inbound_text="mensagem sintética",
            state="active",
            policy_id=None,
            policy_version_id=None,
            correlation_id=f"correlation-{suffix}",
            causation_id=None,
            lia_speech=None,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        InboundEventRow(
            id=event_id,
            source="test",
            external_event_id=f"external-{suffix}",
            event_type="message",
            payload={"safe": True},
            payload_hash=f"hash-{suffix}",
            received_at=now,
            interaction_id=interaction_id,
            status="processed",
            correlation_id=f"correlation-{suffix}",
        )
    )
    row = AgentDecisionRow(
        id=decision_id,
        event_id=event_id,
        interaction_id=interaction_id,
        agent_blueprint_id=None,
        agent_blueprint_version=None,
        actor_id=None,
        actor_binding_id=None,
        audience="unknown",
        policy_version_id=None,
        decision_pipeline_version="v1",
        decision_type="RESPOND",
        recommended_action="soft_ping",
        proposed_response="Resposta sintética proposta pela Andy.",
        escalation_required=False,
        escalation_reason=None,
        confidence=0.9,
        missing_information=[],
        execution_allowed=False,
        external_delivery_allowed=False,
        reasoning_summary="synthetic",
        status="DRY_RUN",
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _authority(authenticated=True) -> OperatorAuthority:
    return OperatorAuthority(
        operator_actor_id="owner-actor",
        authenticated=authenticated,
        roles=["OWNER"],
    )


def test_parser_recognizes_explicit_approve_and_deny_commands():
    reference = "12345678-ab1"

    approve = parse_owner_grace_control(f"Andy, aprovar resposta {reference}")
    deny = parse_owner_grace_control(f"negar resposta {reference}")
    question = parse_owner_grace_control(f"aprovar resposta {reference}?")
    malformed = parse_owner_grace_control("aprovar resposta xyz")

    assert approve.status == OwnerControlParseStatus.MATCHED
    assert approve.action == OwnerControlAction.APPROVE_RESPONSE_REVIEW
    assert approve.parameters.reference == reference
    assert deny.status == OwnerControlParseStatus.MATCHED
    assert deny.action == OwnerControlAction.REJECT_RESPONSE_REVIEW
    assert question.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND
    assert malformed.status == OwnerControlParseStatus.REJECTED
    assert malformed.reason_code == "OWNER_REVIEW_REFERENCE_INVALID"


def test_review_creation_enqueues_one_idempotent_owner_request(session):
    binding = _owner_binding(session)
    decision = _decision(session, suffix="request")

    review = response_review.create_review_for_decision(session, decision.id)
    duplicate = response_review.create_review_for_decision(session, decision.id)
    session.flush()

    requests = session.scalars(
        select(OutboxMessageRow).where(
            OutboxMessageRow.idempotency_key == f"owner-response-review:request:{review.id}"
        )
    ).all()
    assert duplicate.id == review.id
    assert len(requests) == 1
    assert requests[0].action_type == OWNER_REVIEW_REQUEST_ACTION == "owner_control_text"
    assert requests[0].destination == "local_transport"
    assert requests[0].payload["external_actor_id"] == binding.external_actor_id
    assert "Resposta sintética proposta pela Andy." in requests[0].payload["text"]
    assert f"aprovar resposta {review_reference(review.id)}" in requests[0].payload["text"]
    assert f"negar resposta {review_reference(review.id)}" in requests[0].payload["text"]


def test_direct_review_control_requires_authenticated_owner_and_keeps_delivery_blocked(session):
    decision = _decision(session, suffix="approve")
    review = response_review.create_review_for_decision(session, decision.id)
    reference = review_reference(review.id)

    with pytest.raises(OwnerResponseReviewError, match="OWNER_AUTHORITY_UNAVAILABLE"):
        apply_owner_response_review(
            session,
            tenant_id=review_to_tenant(session, review),
            reference=reference,
            approve=True,
            authority=_authority(authenticated=False),
        )

    mutation = apply_owner_response_review(
        session,
        tenant_id=review_to_tenant(session, review),
        reference=reference,
        approve=True,
        authority=_authority(),
    )
    duplicate = apply_owner_response_review(
        session,
        tenant_id=review_to_tenant(session, review),
        reference=reference,
        approve=True,
        authority=_authority(),
    )
    intent = session.get(AgentExecutionIntentRow, mutation.execution_intent_id)

    assert mutation.review_status == "APPROVED"
    assert mutation.changed is True
    assert duplicate.duplicate is True
    assert duplicate.execution_intent_id == mutation.execution_intent_id
    assert intent.status == "BLOCKED"
    assert intent.execution_allowed is False
    assert intent.external_delivery_allowed is False
    assert intent.blocked_reason == "EXTERNAL_DELIVERY_DISABLED"


def review_to_tenant(session, review: AgentResponseReviewRow) -> str:
    decision = session.get(AgentDecisionRow, review.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id)
    return interaction.tenant_id


def test_authenticated_wwebjs_self_chat_approves_pending_review_end_to_end(session):
    binding = _owner_binding(session)
    decision = _decision(session, suffix="e2e")
    review = response_review.create_review_for_decision(session, decision.id)
    reference = review_reference(review.id)

    result = services.receive_inbound_event(
        session,
        source="wwebjs",
        external_event_id="owner-command-review-approve-1",
        event_type="message",
        contact_id=binding.actor_key,
        contact_name="Synthetic Owner",
        relationship_category="owner",
        active_context="owner_control",
        inbound_text=f"aprovar resposta {reference}",
        actor_binding=binding,
        payload={
            "content": f"aprovar resposta {reference}",
            "actor_id": binding.external_actor_id,
            "event_origin": "OWNER_COMMAND",
            "owner_authenticated": True,
            "metadata": {
                "from_me": True,
                "owner_self_chat": True,
                "from_me_classification": "OWNER_COMMAND",
                "final_from_me_classification": "OWNER_COMMAND",
            },
        },
    )
    session.flush()

    updated = session.get(AgentResponseReviewRow, review.id)
    intent = session.scalar(
        select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.response_review_id == review.id)
    )
    confirmations = session.scalars(
        select(OutboxMessageRow).where(
            OutboxMessageRow.idempotency_key == "owner-control:confirmation:" + result["inbound_event_id"]
        )
    ).all()

    assert result["event_type"] == "OWNER_CONTROL_COMMAND"
    assert updated.status == "APPROVED"
    assert intent is not None
    assert intent.external_delivery_allowed is False
    assert len(confirmations) == 1
    assert "Resposta aprovada" in confirmations[0].payload["text"]


def test_deny_is_terminal_and_creates_no_execution_intent(session):
    decision = _decision(session, suffix="deny")
    review = response_review.create_review_for_decision(session, decision.id)

    mutation = apply_owner_response_review(
        session,
        tenant_id=review_to_tenant(session, review),
        reference=review_reference(review.id),
        approve=False,
        authority=_authority(),
    )
    duplicate = apply_owner_response_review(
        session,
        tenant_id=review_to_tenant(session, review),
        reference=review_reference(review.id),
        approve=False,
        authority=_authority(),
    )

    assert mutation.review_status == "REJECTED"
    assert duplicate.duplicate is True
    assert session.scalar(
        select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.response_review_id == review.id)
    ) is None
