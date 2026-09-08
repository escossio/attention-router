from datetime import timedelta

import pytest

from attention_router.application.agents.context import AllowedAgentContext
from attention_router.application.decision_pipeline import _recent_agent_turns
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    VoiceTranscriptionRow,
)


BASE_TIME = now_utc()


def _interaction(
    session,
    *,
    text: str,
    offset: int,
    contact_id: str = "contact-a",
    tenant_id: str = DEFAULT_TENANT_ID,
    voice_transcript: str | None = None,
) -> InteractionRow:
    stamp = BASE_TIME + timedelta(seconds=offset)
    interaction = InteractionRow(
        id=new_id(),
        tenant_id=tenant_id,
        event_type="message",
        contact_id=contact_id,
        contact_name="Contact",
        relationship_category="known_contact",
        active_context=None,
        inbound_text=text,
        state="COMPLETED",
        correlation_id=new_id(),
        causation_id=None,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(interaction)
    session.flush()
    if voice_transcript is not None:
        event = InboundEventRow(
            id=new_id(),
            tenant_id=tenant_id,
            source="wwebjs",
            external_event_id=new_id(),
            event_type="message",
            payload={
                "channel": "whatsapp",
                "message_type": "ptt",
                "metadata": {"message_type": "ptt", "has_media": True},
            },
            payload_hash=new_id(),
            received_at=stamp,
            processed_at=stamp,
            interaction_id=interaction.id,
            status="PROCESSED",
            error=None,
            correlation_id=interaction.correlation_id,
        )
        session.add(event)
        session.flush()
        session.add(
            VoiceTranscriptionRow(
                id=new_id(),
                tenant_id=tenant_id,
                inbound_event_id=event.id,
                media_artifact_id=None,
                status="READY",
                transcript_text=voice_transcript,
                provider="offline",
                model="offline",
                provider_request_reference="offline",
                error_code=None,
                attempt_count=1,
                claimed_at=None,
                claimed_by=None,
                created_at=stamp,
                updated_at=stamp,
                completed_at=stamp,
            )
        )
        session.flush()
    return interaction


def _agent_decision(session, interaction: InteractionRow, proposed: str) -> AgentDecisionRow:
    row = AgentDecisionRow(
        id=new_id(),
        event_id=new_id(),
        interaction_id=interaction.id,
        agent_blueprint_id=None,
        agent_blueprint_version=None,
        actor_id=interaction.contact_id,
        actor_binding_id=None,
        audience="known_contact",
        policy_version_id=None,
        decision_pipeline_version="history-test",
        decision_type="RESPOND",
        recommended_action="respond",
        proposed_response=proposed,
        response_message_family=None,
        response_variant_id=None,
        response_spoken_text=proposed,
        response_introduction_included=False,
        escalation_required=False,
        escalation_reason=None,
        confidence=1.0,
        missing_information=[],
        intent="conversation.reply",
        objective="reply",
        self_contained=True,
        context_sufficient=True,
        context_requirements=[],
        requested_capabilities=[],
        canonical_event_id=None,
        semantic_source="OPENAI_AGENTS_SDK",
        response_source="OPENAI_AGENTS_SDK",
        execution_allowed=True,
        external_delivery_allowed=True,
        reasoning_summary="offline history fixture",
        status="SUCCESS",
        created_at=interaction.created_at,
    )
    session.add(row)
    session.flush()
    return row


def _intent(
    session,
    interaction: InteractionRow,
    *,
    proposed: str = "draft response",
    effective: str = "effective response",
    status: str = "SENT",
) -> AgentExecutionIntentRow:
    decision = _agent_decision(session, interaction, proposed)
    row = AgentExecutionIntentRow(
        id=new_id(),
        agent_decision_id=decision.id,
        response_review_id=None,
        autonomy_evaluation_id=None,
        authorization_source="POLICY_AUTONOMY",
        intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot=effective,
        status=status,
        execution_allowed=True,
        external_delivery_allowed=True,
        blocked_reason=None,
        release_status="RELEASED",
        released_at=interaction.created_at,
        released_by="offline-test",
        recipient_reference=None,
        idempotency_key=f"history-intent:{new_id()}",
        capability_name=None,
        capability_request=None,
        provider_instance_id=None,
        canonical_event_id=None,
        created_at=interaction.created_at,
        executed_at=interaction.created_at if status == "SENT" else None,
        execution_intent_id=None,
        execution_intent_fingerprint=None,
    )
    session.add(row)
    session.flush()
    return row


def _outbox(
    session,
    interaction: InteractionRow,
    intent: AgentExecutionIntentRow,
    *,
    status: str = "DONE",
    voice: bool = False,
) -> OutboxMessageRow:
    row = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction.id,
        action_type="agent_execution_voice" if voice else "agent_execution_text",
        destination="local_transport",
        payload={
            "execution_intent_id": intent.id,
            "message_type": "ptt" if voice else "text",
            **({} if voice else {"text": intent.effective_response_snapshot}),
        },
        status=status,
        created_at=interaction.created_at,
        available_at=interaction.created_at,
        claimed_at=None,
        claimed_by=None,
        attempt_count=1,
        last_error=None,
        completed_at=interaction.created_at if status == "DONE" else None,
        idempotency_key=f"history-outbox:{new_id()}",
        correlation_id=interaction.correlation_id,
        causation_id=intent.agent_decision_id,
        execution_intent_id=intent.id,
    )
    session.add(row)
    session.flush()
    return row


def _history(session, current: InteractionRow) -> list[dict[str, str]]:
    return _recent_agent_turns(
        session,
        current.tenant_id,
        current.contact_id,
        current.id,
    )


def test_delivered_text_response_included_as_assistant(session):
    previous = _interaction(session, text="question", offset=1)
    intent = _intent(session, previous, proposed="draft", effective="delivered text")
    _outbox(session, previous, intent)
    current = _interaction(session, text="follow-up", offset=2)

    assert _history(session, current) == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "delivered text"},
    ]


def test_delivered_voice_response_included_as_assistant_text(session):
    previous = _interaction(session, text="voice question", offset=1)
    intent = _intent(session, previous, effective="text synthesized for voice")
    _outbox(session, previous, intent, voice=True)
    current = _interaction(session, text="follow-up", offset=2)

    assert _history(session, current)[1] == {
        "role": "assistant",
        "content": "text synthesized for voice",
    }


@pytest.mark.parametrize("status", ["FAILED", "AMBIGUOUS", "PENDING"])
def test_non_delivered_outbound_excluded(session, status):
    previous = _interaction(session, text=f"question-{status}", offset=1)
    intent = _intent(session, previous, effective="must not be remembered")
    _outbox(session, previous, intent, status=status)
    current = _interaction(session, text="follow-up", offset=2)

    assert _history(session, current) == [
        {"role": "user", "content": f"question-{status}"}
    ]


def test_suppressed_response_excluded(session):
    previous = _interaction(session, text="question", offset=1)
    _intent(
        session,
        previous,
        effective="suppressed response",
        status="CANCELLED",
    )
    current = _interaction(session, text="follow-up", offset=2)

    assert _history(session, current) == [{"role": "user", "content": "question"}]


def test_generated_but_not_delivered_response_excluded(session):
    previous = _interaction(session, text="question", offset=1)
    _agent_decision(session, previous, "generated only")
    current = _interaction(session, text="follow-up", offset=2)

    assert _history(session, current) == [{"role": "user", "content": "question"}]


def test_unanswered_user_turn_preserved(session):
    _interaction(session, text="unanswered question", offset=1)
    current = _interaction(session, text="why no answer?", offset=2)

    assert _history(session, current) == [
        {"role": "user", "content": "unanswered question"}
    ]


def test_user_assistant_chronological_order_preserved(session):
    first = _interaction(session, text="user A", offset=1)
    first_intent = _intent(session, first, effective="assistant A")
    _outbox(session, first, first_intent)
    _interaction(session, text="user B", offset=2)
    third = _interaction(session, text="user C", offset=3)
    third_intent = _intent(session, third, effective="assistant C")
    _outbox(session, third, third_intent)
    current = _interaction(session, text="current", offset=4)

    assert _history(session, current) == [
        {"role": "user", "content": "user A"},
        {"role": "assistant", "content": "assistant A"},
        {"role": "user", "content": "user B"},
        {"role": "user", "content": "user C"},
        {"role": "assistant", "content": "assistant C"},
    ]


def test_voice_transcript_used_for_user_history(session):
    _interaction(
        session,
        text="[ptt]",
        voice_transcript="semantic voice transcript",
        offset=1,
    )
    current = _interaction(session, text="follow-up", offset=2)

    history = _history(session, current)
    assert history == [{"role": "user", "content": "semantic voice transcript"}]
    assert "media_ref" not in str(history)


def test_contact_isolation_preserved(session):
    other = _interaction(session, text="contact B private", contact_id="contact-b", offset=1)
    other_intent = _intent(session, other, effective="contact B answer")
    _outbox(session, other, other_intent)
    _interaction(session, text="contact A previous", offset=2)
    current = _interaction(session, text="contact A current", offset=3)

    serialized = str(_history(session, current))
    assert "contact B" not in serialized
    assert "contact A previous" in serialized


def test_current_interaction_excluded_from_history(session):
    _interaction(session, text="previous", offset=1)
    current = _interaction(session, text="current must stay separate", offset=2)

    assert _history(session, current) == [{"role": "user", "content": "previous"}]


def test_future_interaction_and_assistant_excluded_when_reprocessing_old_event(session):
    past = _interaction(session, text="past", offset=1)
    current = _interaction(session, text="current", offset=2)
    future = _interaction(session, text="future", offset=3)
    future_intent = _intent(session, future, effective="future assistant")
    _outbox(session, future, future_intent, status="DONE")

    assert _history(session, current) == [{"role": "user", "content": past.inbound_text}]


def test_equal_timestamp_is_excluded_fail_closed(session):
    past = _interaction(session, text="past", offset=1)
    current = _interaction(session, text="current", offset=2)
    same_time = _interaction(session, text="same timestamp", offset=3)
    same_time.created_at = current.created_at
    session.commit()

    assert _history(session, current) == [{"role": "user", "content": past.inbound_text}]


def test_tenant_isolation_preserved_with_temporal_cutoff(session):
    tenant_a = _interaction(session, text="tenant a", offset=1, tenant_id="tenant-a")
    _interaction(session, text="tenant b", offset=2, tenant_id="tenant-b")
    current = _interaction(session, text="current", offset=3, tenant_id="tenant-a")

    assert _history(session, current) == [{"role": "user", "content": tenant_a.inbound_text}]


def test_six_interaction_limit_preserved(session):
    for offset in range(1, 8):
        previous = _interaction(session, text=f"user {offset}", offset=offset)
        intent = _intent(session, previous, effective=f"assistant {offset}")
        _outbox(session, previous, intent)
    current = _interaction(session, text="current", offset=8)

    history = _history(session, current)
    assert len(history) == 12
    assert history[0] == {"role": "user", "content": "user 2"}
    assert history[-1] == {"role": "assistant", "content": "assistant 7"}
    assert "user 1" not in {entry["content"] for entry in history}


def test_bidirectional_context_simulation_preserves_roles_in_prompt_payload(session):
    first = _interaction(
        session,
        text="Escolha uma palavra entre cacto e farol.",
        offset=1,
    )
    intent = _intent(session, first, effective="cacto")
    _outbox(session, first, intent)
    current = _interaction(
        session,
        text="Qual palavra você acabou de me responder?",
        offset=2,
    )
    context = AllowedAgentContext(
        actor_id=None,
        binding_id=None,
        audience="known_contact",
        policy_summary="offline",
        recent_turns=_history(session, current),
        current_message=current.inbound_text,
    )

    payload = context.prompt_payload()
    assert payload["recent_turns"] == [
        {"role": "user", "content": "Escolha uma palavra entre cacto e farol."},
        {"role": "assistant", "content": "cacto"},
    ]
    assert payload["current_message"] == "Qual palavra você acabou de me responder?"
