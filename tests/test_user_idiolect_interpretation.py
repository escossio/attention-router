from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from attention_router.application import services
from attention_router.application.owner_control import OWNER_CONTROL_SOURCE_CHANNEL
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    FactRow,
    OutboxMessageRow,
    OwnerOperationalControlRow,
    PendingIntentRow,
)
from tests.test_owner_control import OWNER_ACTOR_ID, _install_owner_and_grace
from tests.test_owner_control_clarification import (
    CONVERSATION,
    _event,
    _install_semantic_mocks,
)


def _confirmed_fact(
    session,
    *,
    semantic_intent_key: str,
    parameters: dict[str, object],
    conversation: str = CONVERSATION,
) -> FactRow:
    stamp = now_utc()
    row = FactRow(
        id=new_id(),
        tenant_id="00000000-0000-4000-8000-000000000001",
        subject_type="ACTOR",
        subject_id=OWNER_ACTOR_ID,
        predicate="idiolect.pragmatic_mapping",
        value_json={
            "expression": "retorne em 30 segundos",
            "normalized_expression": "retorne em 30 segundos",
            "meaning_kind": "SEMANTIC_INTENT",
            "semantic_intent_key": semantic_intent_key,
            "parameters": parameters,
            "context_scope": {
                "channel": OWNER_CONTROL_SOURCE_CHANNEL,
                "conversation_key_hash": stable_hash(conversation),
            },
            "direction": "USER_TO_ANDY_LANGUAGE",
            "evidence_class": "EXPLICITLY_CONFIRMED",
            "reuse_policy": "INTERPRET_ONLY",
            "generalization_scope": "CONVERSATION",
            "evidence_confidence": 1.0,
            "generalization_confidence": 0.25,
        },
        value_ref=None,
        fact_class="USER_CONFIRMED_LANGUAGE",
        source_type="INTENT_CLARIFICATION",
        source_ref=f"pending-{new_id()}",
        confidence=1.0,
        observed_at=stamp,
        valid_from=stamp - timedelta(seconds=1),
        valid_until=stamp + timedelta(days=7),
        supersedes_fact_id=None,
        metadata_json={"sensitivity": "PRIVATE"},
        created_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def _outbox(session, interaction_id: str) -> OutboxMessageRow:
    row = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == interaction_id
        )
    )
    assert row is not None
    return row


def test_exact_confirmed_available_meaning_avoids_repeat_clarification(
    session,
    monkeypatch,
):
    _install_owner_and_grace(session)
    _install_semantic_mocks(monkeypatch)
    _confirmed_fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )

    result = services.receive_normalized_inbound_event(
        session,
        _event("idiolect-v1f-available", "retorne em 30 segundos"),
    )

    control = session.scalar(select(OwnerOperationalControlRow))
    assert control is not None
    assert control.integer_value == 30
    assert session.scalar(select(PendingIntentRow)) is None
    assert _outbox(session, result["id"]).payload["text"] == (
        "Espera alterada para 30 segundos."
    )


def test_conflicting_confirmed_meanings_still_clarify(session, monkeypatch):
    _install_owner_and_grace(session)
    _install_semantic_mocks(monkeypatch)
    _confirmed_fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )
    _confirmed_fact(
        session,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )

    result = services.receive_normalized_inbound_event(
        session,
        _event("idiolect-v1f-conflict", "retorne em 30 segundos"),
    )

    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    assert pending.state == "PENDING"
    assert session.scalar(select(OwnerOperationalControlRow)) is None
    assert "1)" in _outbox(session, result["id"]).payload["text"]


def test_confirmed_unavailable_meaning_still_clarifies(session, monkeypatch):
    _install_owner_and_grace(session)
    _install_semantic_mocks(monkeypatch)
    _confirmed_fact(
        session,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )

    services.receive_normalized_inbound_event(
        session,
        _event("idiolect-v1f-unavailable", "retorne em 30 segundos"),
    )

    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    assert pending.state == "PENDING"
    assert session.scalar(select(OwnerOperationalControlRow)) is None


def test_confirmed_meaning_from_other_conversation_does_not_apply(
    session,
    monkeypatch,
):
    _install_owner_and_grace(session)
    _install_semantic_mocks(monkeypatch)
    _confirmed_fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
        conversation="wwebjs:other-owner-thread@c.us",
    )

    services.receive_normalized_inbound_event(
        session,
        _event("idiolect-v1f-other-conversation", "retorne em 30 segundos"),
    )

    pending = session.scalar(select(PendingIntentRow))
    assert pending is not None
    assert pending.state == "PENDING"
    assert session.scalar(select(OwnerOperationalControlRow)) is None
