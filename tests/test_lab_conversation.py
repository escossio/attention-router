from datetime import datetime, timezone

import pytest

from attention_router.application import lab_conversation
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
)


def test_session_claim_is_bounded_and_stop_blocks_claims(session):
    binding = ActorBindingRow(
        id="binding-lab", source="wwebjs", external_actor_id="actor@lid",
        actor_key="actor", actor_category="unknown", binding_metadata={},
        is_active=True, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    session.add(binding)
    session.flush()
    lab = lab_conversation.start_session(session, "binding-lab", "autonomy_canary_v1", max_inbounds=1, max_sends=1)
    event = InboundEventRow(
        id="event-lab", source="wwebjs", external_event_id="lab-event", event_type="message",
        payload={"channel": "whatsapp"}, payload_hash="hash", received_at=datetime.now(timezone.utc),
        interaction_id=None, status="processed", correlation_id="lab-correlation",
    )
    session.add(event)
    session.flush()
    assert lab_conversation.claim_inbound(session, event, "binding-lab", "autonomy_canary_v1").ordinal == 1
    assert lab_conversation.claim_inbound(session, event, "binding-lab", "autonomy_canary_v1").ordinal == 1
    second = InboundEventRow(
        id="event-lab-second", source="wwebjs", external_event_id="lab-event-second", event_type="message",
        payload={"channel": "whatsapp"}, payload_hash="hash-second", received_at=datetime.now(timezone.utc),
        interaction_id=None, status="processed", correlation_id="lab-correlation-second",
    )
    session.add(second)
    session.flush()
    lab.max_inbounds = 2
    assert lab_conversation.claim_inbound(session, second, "binding-lab", "autonomy_canary_v1").ordinal == 2
    lab_conversation.stop_session(session, lab.id)
    next_event = InboundEventRow(
        id="event-lab-2", source="wwebjs", external_event_id="lab-event-2", event_type="message",
        payload={"channel": "whatsapp"}, payload_hash="hash-2", received_at=datetime.now(timezone.utc),
        interaction_id=None, status="processed", correlation_id="lab-correlation-2",
    )
    session.add(next_event)
    session.flush()
    assert lab_conversation.claim_inbound(session, next_event, "binding-lab", "autonomy_canary_v1") is None


def test_second_active_session_same_binding_is_blocked(session):
    lab_conversation.start_session(session, "binding-lab", "autonomy_canary_v1")
    with pytest.raises(ValueError, match="active lab session"):
        lab_conversation.start_session(session, "binding-lab", "autonomy_canary_v1")
