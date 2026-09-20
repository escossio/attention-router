from __future__ import annotations

from datetime import timedelta

from attention_router.application.user_idiolect import (
    retrieve_idiolect_interpretation_evidence,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import FactRow
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR_A = "owner-idiolect-a"
ACTOR_B = "owner-idiolect-b"
CONVERSATION = "conversation-hash-a"


def _install_actor(session, actor_key: str, external: str) -> None:
    upsert_actor_binding(
        session,
        "test",
        external,
        actor_key,
        "owner",
        metadata={"owner": True},
    )


def _fact(
    session,
    *,
    actor_key: str = ACTOR_A,
    expression: str = "retorne em 30 segundos",
    semantic_intent_key: str = "ONE_SHOT_REPLY_DELAY",
    parameters: dict[str, object] | None = None,
    conversation_key_hash: str = CONVERSATION,
    channel: str = "wwebjs-owner-control",
    valid_until=None,
) -> FactRow:
    stamp = now_utc()
    row = FactRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        subject_type="ACTOR",
        subject_id=actor_key,
        predicate="idiolect.pragmatic_mapping",
        value_json={
            "expression": expression,
            "normalized_expression": "retorne em 30 segundos",
            "meaning_kind": "SEMANTIC_INTENT",
            "semantic_intent_key": semantic_intent_key,
            "parameters": parameters or {"seconds": 30},
            "context_scope": {
                "channel": channel,
                "conversation_key_hash": conversation_key_hash,
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
        valid_until=valid_until,
        supersedes_fact_id=None,
        metadata_json={"sensitivity": "PRIVATE"},
        created_at=stamp,
    )
    session.add(row)
    session.flush()
    return row


def test_exact_confirmed_mapping_is_retrieved_in_same_context(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    fact = _fact(session)

    items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="Retorne em 30 segundos!",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert len(items) == 1
    assert items[0].fact_id == fact.id
    assert items[0].semantic_intent_key == "ONE_SHOT_REPLY_DELAY"
    assert items[0].parameters == {"seconds": 30}
    assert items[0].direction == "USER_TO_ANDY_LANGUAGE"
    assert items[0].reuse_policy == "INTERPRET_ONLY"


def test_conversation_and_actor_scope_are_fail_closed(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    _install_actor(session, ACTOR_B, "owner-b-ext")
    _fact(session)

    wrong_conversation = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash="other-conversation",
    )
    wrong_actor = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_B,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert wrong_conversation == ()
    assert wrong_actor == ()


def test_expired_confirmed_mapping_is_not_retrieved(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    _fact(session, valid_until=now_utc() - timedelta(seconds=1))

    items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert items == ()


def test_conflicting_confirmed_mappings_are_returned_without_forced_choice(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    _fact(
        session,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )
    _fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )

    items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert {
        (item.semantic_intent_key, tuple(sorted(item.parameters.items())))
        for item in items
    } == {
        ("ONE_SHOT_REPLY_DELAY", (("seconds", 30),)),
        ("CONFIGURE_OWNER_REPLY_GRACE", (("seconds", 30),)),
    }
