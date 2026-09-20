from __future__ import annotations

from datetime import timedelta

from attention_router.application.user_idiolect import (
    EXPLICIT_CONFIRMED_PRECEDENCE,
    REPEATED_OBSERVED_PRECEDENCE,
    retrieve_idiolect_context,
    retrieve_idiolect_interpretation_evidence,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import FactRow, MemoryActorRow, MemoryClaimRow, TenantRow
from attention_router.infrastructure.repository import upsert_actor_binding


ACTOR_A = "owner-idiolect-a"
ACTOR_B = "owner-idiolect-b"
CONVERSATION = "conversation-hash-a"


def _ensure_tenant(session, tenant_id: str) -> None:
    if session.get(TenantRow, tenant_id) is not None:
        return
    stamp = now_utc()
    session.add(
        TenantRow(
            id=tenant_id,
            slug=tenant_id,
            name=tenant_id,
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.flush()


def _install_actor(
    session,
    actor_key: str,
    external: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    _ensure_tenant(session, tenant_id)
    upsert_actor_binding(
        session,
        "test",
        external,
        actor_key,
        "owner",
        metadata={"owner": True},
        tenant_id=tenant_id,
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
    supersedes_fact_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    direction: str = "USER_TO_ANDY_LANGUAGE",
    reuse_policy: str = "INTERPRET_ONLY",
    evidence_confidence: float = 1.0,
) -> FactRow:
    stamp = now_utc()
    row = FactRow(
        id=new_id(),
        tenant_id=tenant_id,
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
            "direction": direction,
            "evidence_class": "EXPLICITLY_CONFIRMED",
            "reuse_policy": reuse_policy,
            "generalization_scope": "CONVERSATION",
            "evidence_confidence": evidence_confidence,
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
        supersedes_fact_id=supersedes_fact_id,
        metadata_json={"sensitivity": "PRIVATE"},
        created_at=stamp,
    )
    session.add(row)
    session.flush()
    return row




def _observed_claim(
    session,
    *,
    actor_key: str = "owner-a-ext",
    predicate: str = "communication.observed.profanity_tolerance",
    valid_until=None,
) -> MemoryClaimRow:
    stamp = now_utc()
    actor = MemoryActorRow(
        id=new_id(),
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=actor_key,
        metadata_json={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(actor)
    session.flush()
    claim = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=predicate,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "signal": "PROFANITY_PRESENT",
            "direction": "USER_TO_ANDY_LANGUAGE",
            "evidence_class": "OBSERVED",
            "reuse_policy": "INTERPRET_ONLY",
            "generalization_scope": "PERSON",
            "generalization_confidence": 0.25,
        },
        context={
            "promotion_rule": "RECURRENCE_V1",
            "observation_threshold": 3,
            "observation_count": 3,
        },
        confidence=0.70,
        sensitivity_class="NORMAL",
        source_quality="REPEATED_OBSERVATION",
        valid_from=stamp - timedelta(days=1),
        valid_until=valid_until or stamp + timedelta(days=30),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=None,
        conflict_group_id=None,
        first_observed_at=stamp - timedelta(days=3),
        last_observed_at=stamp,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(claim)
    session.flush()
    return claim


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


def test_superseded_confirmed_mapping_is_excluded_from_retrieval(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    old = _fact(
        session,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )
    new = _fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
        supersedes_fact_id=old.id,
    )

    items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert len(items) == 1
    assert items[0].fact_id == new.id
    assert items[0].semantic_intent_key == "CONFIGURE_OWNER_REPLY_GRACE"



def test_context_retrieval_ranks_explicit_confirmation_above_observed_pattern(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    fact = _fact(
        session,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )
    claim = _observed_claim(session)

    items = retrieve_idiolect_context(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert len(items) == 2
    assert items[0].item_id == fact.id
    assert items[0].evidence_kind == "EXPLICIT_CONFIRMED_MAPPING"
    assert items[0].precedence == EXPLICIT_CONFIRMED_PRECEDENCE
    assert items[1].item_id == claim.id
    assert items[1].evidence_kind == "REPEATED_OBSERVED_PATTERN"
    assert items[1].precedence == REPEATED_OBSERVED_PRECEDENCE
    assert items[1].evidence_confidence == 0.70
    assert items[1].generalization_confidence == 0.25
    assert items[1].reuse_policy == "INTERPRET_ONLY"


def test_context_retrieval_excludes_expired_observed_pattern(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    _observed_claim(
        session,
        valid_until=now_utc() - timedelta(seconds=1),
    )

    items = retrieve_idiolect_context(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="mensagem qualquer",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert items == ()


def test_context_retrieval_resolves_observed_pattern_through_binding_alias(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    claim = _observed_claim(session, actor_key="owner-a-ext")

    items = retrieve_idiolect_context(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="mensagem qualquer",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert len(items) == 1
    assert items[0].item_id == claim.id


def test_repeated_observed_pattern_cannot_become_semantic_parse_evidence(session):
    _install_actor(session, ACTOR_A, "owner-a-ext")
    _observed_claim(session)

    interpretation = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )
    context = retrieve_idiolect_context(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert interpretation == ()
    assert len(context) == 1
    assert context[0].evidence_kind == "REPEATED_OBSERVED_PATTERN"
    assert context[0].reuse_policy == "INTERPRET_ONLY"



def test_same_phrase_can_have_different_meanings_in_two_tenants(session):
    tenant_b = "00000000-0000-4000-8000-0000000000b2"
    shared_actor = "owner-shared-cross-tenant"

    _install_actor(
        session,
        shared_actor,
        "owner-tenant-a",
        tenant_id=DEFAULT_TENANT_ID,
    )
    _install_actor(
        session,
        shared_actor,
        "owner-tenant-b",
        tenant_id=tenant_b,
    )
    _fact(
        session,
        actor_key=shared_actor,
        tenant_id=DEFAULT_TENANT_ID,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )
    _fact(
        session,
        actor_key=shared_actor,
        tenant_id=tenant_b,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )

    tenant_a_items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=shared_actor,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )
    tenant_b_items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=tenant_b,
        actor_key=shared_actor,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert [item.semantic_intent_key for item in tenant_a_items] == [
        "CONFIGURE_OWNER_REPLY_GRACE"
    ]
    assert [item.semantic_intent_key for item in tenant_b_items] == [
        "ONE_SHOT_REPLY_DELAY"
    ]


def test_same_phrase_can_have_different_meanings_for_two_people(session):
    _install_actor(session, ACTOR_A, "owner-person-a")
    _install_actor(session, ACTOR_B, "owner-person-b")
    _fact(
        session,
        actor_key=ACTOR_A,
        semantic_intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )
    _fact(
        session,
        actor_key=ACTOR_B,
        semantic_intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )

    actor_a_items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_A,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )
    actor_b_items = retrieve_idiolect_interpretation_evidence(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        actor_key=ACTOR_B,
        utterance="retorne em 30 segundos",
        source_channel="wwebjs-owner-control",
        conversation_key_hash=CONVERSATION,
    )

    assert [item.semantic_intent_key for item in actor_a_items] == [
        "CONFIGURE_OWNER_REPLY_GRACE"
    ]
    assert [item.semantic_intent_key for item in actor_b_items] == [
        "ONE_SHOT_REPLY_DELAY"
    ]


def test_outbound_preference_never_enters_user_to_andy_interpretation(session):
    _install_actor(session, ACTOR_A, "owner-directionality")
    _fact(
        session,
        direction="ANDY_TO_USER_PREFERENCE",
        reuse_policy="EXPLICIT_REUSE",
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

    assert items == ()


def test_style_signal_never_enters_semantic_interpretation(session):
    _install_actor(session, ACTOR_A, "owner-style-signal")
    _fact(
        session,
        direction="USER_TO_ANDY_LANGUAGE",
        reuse_policy="STYLE_SIGNAL",
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

    assert items == ()
