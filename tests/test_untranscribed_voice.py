from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.application import services
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.application.owner_reply_grace import process_due_grace_windows
from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AuditEventRow,
    ConversationResponseGraceWindowRow,
    OutboxMessageRow,
)
from tests.test_direct_conversation import PEER, direct_event, setup_direct


def _voice_event(message_type: str, *, content: str | None = None):
    return InternalIngressAdapter().normalize(
        {
            "source": "wwebjs",
            "external_event_id": new_id(),
            "event_type": "message",
            "external_actor_id": PEER,
            "channel": "whatsapp",
            "message_type": message_type,
            "content": content or "",
            "has_media": True,
            "event_origin": "EXTERNAL_INBOUND",
            "owner_authenticated": False,
            "metadata": {
                "from_me": False,
                "source_account": "default",
                "conversation_state": "READY",
                "conversation_key": "wwebjs:" + PEER,
                "peer_identifiers": [PEER],
            },
        },
    )


@pytest.mark.parametrize("message_type", ["ptt", "audio"])
def test_untranscribed_voice_opens_grace_but_never_reaches_agent_or_effect(
    session, monkeypatch, message_type
):
    setup_direct(session, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "attention_router.application.decision_pipeline.run_andy",
        lambda context: calls.append(context),
    )

    received = services.receive_normalized_inbound_event(
        session, _voice_event(message_type)
    )
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    assert window is not None
    assert window.effective_grace_seconds == 10
    assert (
        process_due_grace_windows(
            session, "voice-test", timestamp=window.due_at + timedelta(seconds=1)
        )
        == 1
    )

    assert process_agent_decision(session, received["inbound_event_id"]) is None
    assert process_agent_decision(session, received["inbound_event_id"]) is None
    assert calls == []
    assert session.scalar(select(AgentDecisionRow)) is None
    assert session.scalar(select(AgentExecutionIntentRow)) is None
    assert session.scalar(select(OutboxMessageRow)) is None
    assert session.scalar(
        select(func.count()).select_from(AuditEventRow).where(
            AuditEventRow.event_type == "voice.transcription_required",
            AuditEventRow.interaction_id == received["id"],
        )
    ) == 1


def test_voice_caption_is_not_treated_as_trusted_transcript(session, monkeypatch):
    setup_direct(session, monkeypatch)
    monkeypatch.setattr(
        "attention_router.application.decision_pipeline.run_andy",
        lambda context: pytest.fail("caption reached Andy as a transcript"),
    )
    received = services.receive_normalized_inbound_event(
        session, _voice_event("ptt", content="Legenda não é transcrição")
    )
    assert process_agent_decision(session, received["inbound_event_id"]) is None
    assert session.scalar(select(AgentDecisionRow)) is None


def test_text_conversation_remains_unchanged(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, direct_event())
    decision = process_agent_decision(session, received["inbound_event_id"])
    assert decision is not None and decision.proposed_response
    assert len(captured) == 1


def test_manual_owner_reply_cancels_voice_grace_and_voice_remains_held(
    session, monkeypatch
):
    setup_direct(session, monkeypatch)
    received = services.receive_normalized_inbound_event(session, _voice_event("ptt"))
    window = session.scalar(select(ConversationResponseGraceWindowRow))
    observation_at = window.last_inbound_at + timedelta(seconds=1)
    observation = direct_event(
        "5500000000027@c.us",
        external_event_id=new_id(),
        content="[owner outbound observation]",
        occurred_at=observation_at,
        received_at=observation_at,
        event_origin="OWNER_MANUAL_OUTBOUND_OBSERVED",
        metadata={
            "from_me": True,
            "source_account": "default",
            "conversation_state": "READY",
            "conversation_key": "wwebjs:" + PEER,
            "peer_identifiers": [PEER, "5500000000027@c.us"],
            "from_me_classification": "OWNER_MANUAL_OUTBOUND_OBSERVED",
        },
    )
    services.receive_normalized_inbound_event(session, observation)

    assert window.state == "CANCELED"
    assert window.cancellation_reason == "OWNER_MANUAL_REPLY"
    assert process_due_grace_windows(
        session, "voice-test", timestamp=window.due_at + timedelta(seconds=1)
    ) == 0
    assert process_agent_decision(session, received["inbound_event_id"]) is None
    assert session.scalar(select(AgentDecisionRow)) is None
    assert session.scalar(select(AgentExecutionIntentRow)) is None
    assert session.scalar(select(OutboxMessageRow)) is None
