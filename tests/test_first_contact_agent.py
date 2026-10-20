"""Synthetic Agent/Transport integration; no provider, Browser or live database."""

from types import SimpleNamespace
from copy import deepcopy
import hashlib
import sys

import pytest
from sqlalchemy import func, select

from attention_router.adapters.wwebjs_outbound import WwebjsOutboundError
from attention_router.application import autonomy, execution, first_contact as fc, response_review, services
from attention_router.application import decision_pipeline as pipeline
from attention_router.application.agents.andy_agent import build_andy_agent
from attention_router.application.agents.first_contact import render_public_identity_response
from attention_router.application.agents.service import AndyAgentError
from attention_router.application.first_contact_runtime import reconcile_presentations
from attention_router.application.voice_tts import process_tts_derivations, source_text_hash, validate_voice_outbox
from attention_router.application import first_contact_runtime
from attention_router.config import Settings, settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.first_contact_models import (
    FirstContactAttemptRow, FirstContactPresentationRow,
)
from attention_router.infrastructure.models import (
    ActorBindingRow, CapabilityGrantRow, OutboxMessageRow, AgentExecutionIntentRow,
    AgentResponseReviewRow, PolicyRow, PolicyVersionRow,
)
from attention_router.infrastructure.repository import ensure_policy_version, upsert_actor_binding
from tests.test_direct_conversation import PEER, direct_event
from tests.test_first_contact import signed_proof
from tests.test_whatsapp_voice_flow import FakeTTS
from tests.test_visitor_default_runtime_guard import setup_guarded_visitor, visitor_decision


def enabled(session, monkeypatch):
    captured, _, _ = setup_guarded_visitor(session, monkeypatch)
    monkeypatch.setattr(settings, "first_contact_enabled", True)
    monkeypatch.setattr(settings, "first_contact_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "first_contact_source_account", "default")
    monkeypatch.setattr(settings, "first_contact_canary_peer_sha256", hashlib.sha256(PEER.encode()).hexdigest())
    session.commit()
    return captured


def enabled_open_inbound(session, monkeypatch):
    captured, _, _ = setup_guarded_visitor(session, monkeypatch)
    monkeypatch.setattr(settings, "first_contact_enabled", True)
    monkeypatch.setattr(settings, "first_contact_eligibility_mode", "OPEN_INBOUND")
    monkeypatch.setattr(settings, "first_contact_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "first_contact_source_account", "default")
    monkeypatch.setattr(settings, "first_contact_canary_peer_sha256", None)
    session.commit()
    return captured


def event(*, peer=PEER, proof=True, **changes):
    item = direct_event(peer=peer, **changes)
    if proof:
        item.metadata.update({
            "conversation_key": "wwebjs:" + PEER,
            "peer_identifiers": list(dict.fromkeys([PEER, peer])),
            "first_contact_identity": {
                "contract_version": "provider-lid-v1", "state": "VERIFIED",
                "canonical_peer_id": PEER,
                "provenance": "DIRECTIONAL_LID" if peer == PEER else "PROVIDER_LID_PN_MAPPING",
            },
        })
    return item


def open_event(peer, **changes):
    item = event(peer=peer, proof=False, **changes)
    item.metadata.update({
        "conversation_key": "wwebjs:" + peer,
        "peer_identifiers": [peer],
        "first_contact_identity": {
            "contract_version": "provider-lid-v1", "state": "VERIFIED",
            "canonical_peer_id": peer, "provenance": "DIRECTIONAL_LID",
        },
    })
    return item


def enqueue(session, decision):
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    session.commit()
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    session.commit()
    return session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.causation_id == decision.id,
    ))


def send(session, monkeypatch, outbox):
    calls = []

    def dispatch(row):
        calls.append(row.id)
        return SimpleNamespace(status="sent", response={"message_reference": "synthetic-fc-message"})

    monkeypatch.setattr(services.local_transport_outbound, "dispatch_outbox", dispatch)
    # A human review may also have its existing operator-notification outbox.
    assert services.process_outbox(session, "synthetic-fc-worker") >= 1
    assert calls.count(outbox.id) == 1 and outbox.status == "DONE"
    assert session.scalar(select(FirstContactPresentationRow)).state == "SENT"
    return calls


def confirm(session, outbox, ack=2):
    proof = signed_proof(session, SimpleNamespace(outbox=outbox), ack=ack)
    result = fc.confirm_delivery(session, outbox.id, proof)
    session.commit()
    return result


def test_intro_supplemental_audio_waits_for_text_ack_and_keeps_visitor_guard(
    session, monkeypatch, tmp_path,
):
    captured = enabled(session, monkeypatch)
    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_auto_send", True)
    monkeypatch.setattr(settings, "tts_supplemental_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    decision = visitor_decision(session, event(content="Olá"))
    text = enqueue(session, decision)
    intent = session.get(AgentExecutionIntentRow, text.execution_intent_id)
    assert text.payload["text"].startswith("[Andy • IA]")
    assert text.payload["supplemental_audio_text_hash"] == source_text_hash(text.payload["text"])
    assert captured[0].available_capabilities == []
    fake = FakeTTS()
    assert process_tts_derivations(session, "tts", client=fake) == 0
    send(session, monkeypatch, text)
    assert process_tts_derivations(session, "tts", client=fake) == 0
    assert not confirm(session, text, ack=1)
    assert process_tts_derivations(session, "tts", client=fake) == 0
    assert confirm(session, text, ack=2)
    assert process_tts_derivations(session, "tts", client=fake) == 1
    assert fake.texts and not fake.texts[0].startswith("[Andy • IA]")
    voice = session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.idempotency_key == f"execution:{intent.id}:audio"
    ))
    assert voice.payload["source_text_hash"] == source_text_hash(intent.effective_response_snapshot)
    assert voice.payload["parent_outbox_id"] == text.id
    validate_voice_outbox(session, voice, intent)
    calls = []

    def send_audio(row):
        calls.append(row.id)
        return SimpleNamespace(status="sent", response={"message_reference": "synthetic-fc-audio"})

    monkeypatch.setattr(services.local_transport_outbound, "dispatch_outbox", send_audio)
    services.process_outbox(session, "synthetic-audio-worker")
    assert calls == [voice.id] and voice.status == "DONE" and text.status == "DONE"
    assert session.scalar(select(FirstContactPresentationRow)).state == "CONFIRMED"
    assert process_tts_derivations(session, "tts", client=fake) == 0
    assert fake.calls == 1 and len(captured) == 1
    next_decision = visitor_decision(session, event(content="E depois?"))
    assert next_decision.response_introduction_included is False
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    continued = enqueue(session, next_decision)
    assert "supplemental_audio_text_hash" not in continued.payload
    continued.status = "DONE"
    session.commit()
    assert process_tts_derivations(session, "tts", client=fake) == 0
    assert fake.calls == 1
    assert len(session.scalars(select(OutboxMessageRow).where(
        OutboxMessageRow.action_type == "agent_execution_voice"
    )).all()) == 1


@pytest.mark.parametrize("case", ["linked_actor", "already_presented", "unconfirmed", "ambiguous"])
def test_non_intro_or_unconfirmed_first_contact_never_enqueues_audio(
    session, monkeypatch, tmp_path, case,
):
    enabled(session, monkeypatch)
    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_auto_send", True)
    monkeypatch.setattr(settings, "tts_supplemental_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    if case == "linked_actor":
        phone = "5500000000027@c.us"
        upsert_actor_binding(session, "wwebjs", phone, "synthetic-owner", "owner",
                             metadata={"owner": True, "audience": "owner"})
        session.commit()
    if case == "already_presented":
        first = enqueue(session, visitor_decision(session, event()))
        send(session, monkeypatch, first)
        assert confirm(session, first)
        assert process_tts_derivations(session, "tts", client=FakeTTS()) == 1
    incoming = event()
    if case == "linked_actor":
        incoming.metadata["peer_identifiers"] = [PEER, phone]
        incoming.metadata["first_contact_identity"]["provenance"] = "PROVIDER_LID_PN_MAPPING"
    decision = visitor_decision(session, incoming)
    if case == "linked_actor":
        assert decision.response_introduction_included is not True
    else:
        assert decision.response_introduction_included is (case in ("unconfirmed", "ambiguous"))
    text = enqueue(session, decision)
    intent = session.get(AgentExecutionIntentRow, text.execution_intent_id)
    if case in ("linked_actor", "already_presented"):
        assert "supplemental_audio_text_hash" not in text.payload
    if case == "ambiguous":
        text.status = "AMBIGUOUS"
        first_contact_runtime.observe_presentation_outbox(session, text)
    else:
        text.status = "DONE"
    session.commit()
    assert process_tts_derivations(session, "tts", client=FakeTTS()) == 0
    assert session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.idempotency_key == f"execution:{intent.id}:audio"
    )) is None


def test_first_contact_flows_through_agent_intent_outbox_but_done_is_not_confirmation(
    session, monkeypatch,
):
    captured = enabled(session, monkeypatch)
    bindings_before = session.scalar(select(func.count()).select_from(ActorBindingRow))
    first = visitor_decision(session, event())
    assert first.response_introduction_included is True
    assert first.proposed_response.startswith("[Andy • IA] Olá! Sou a Andy, uma assistente virtual")
    assert first.proposed_response.count("[Andy • IA]") == 1
    assert captured[0].first_contact["mode"] == "INTRODUCE"
    assert captured[0].represented_subject is None
    assert captured[0].available_capabilities == [] and captured[0].allowed_disclosures == []
    assert session.scalar(select(func.count()).select_from(ActorBindingRow)) == bindings_before
    assert session.scalar(select(func.count()).select_from(CapabilityGrantRow)) == 0
    outbox = enqueue(session, first)
    assert outbox.action_type == "agent_execution_text"
    assert outbox.payload["first_contact"] == fc.delivery_scope(session, outbox.id)
    send(session, monkeypatch, outbox)
    state = session.scalar(select(FirstContactPresentationRow))
    assert state.confirmed_at is None
    assert not confirm(session, outbox, 1)
    assert state.state == "SENT" and state.confirmed_at is None
    assert confirm(session, outbox, 2)
    assert state.state == "CONFIRMED" and state.confirmed_at is not None
    second = visitor_decision(session, event())
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert second.proposed_response.startswith("[Andy • IA] Fibra")
    assert second.proposed_response.count("[Andy • IA]") == 1
    assert "Olá!" not in second.proposed_response
    assert second.response_introduction_included is False


def test_explicit_identity_after_confirmation_is_not_a_new_inauguration(session, monkeypatch):
    captured = enabled(session, monkeypatch)
    first = visitor_decision(session, event())
    outbox = enqueue(session, first)
    send(session, monkeypatch, outbox)
    assert confirm(session, outbox)
    question = visitor_decision(session, event(content="Quem é você?"))
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert question.proposed_response.startswith("[Andy • IA] Sou a Andy, uma assistente virtual")
    assert "Olá!" not in question.proposed_response
    assert question.response_introduction_included is False
    assert session.scalar(select(FirstContactPresentationRow)).state == "CONFIRMED"


def test_open_inbound_introduces_each_proven_direct_visitor_once(session, monkeypatch):
    captured = enabled_open_inbound(session, monkeypatch)
    first_peer = "1234567890@lid"
    second_peer = "2234567890@lid"
    first = visitor_decision(session, open_event(first_peer))
    assert first.response_introduction_included is True
    assert captured[-1].first_contact["mode"] == "INTRODUCE"
    other = visitor_decision(session, open_event(second_peer))
    assert other.response_introduction_included is True
    assert captured[-1].first_contact["mode"] == "INTRODUCE"
    repeat = visitor_decision(session, open_event(first_peer))
    assert repeat.response_introduction_included is False
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 2


def test_open_inbound_confirmed_state_survives_for_next_interaction(session, monkeypatch):
    captured = enabled_open_inbound(session, monkeypatch)
    incoming = open_event("5234567890@lid")
    first = visitor_decision(session, incoming)
    outbox = enqueue(session, first)
    send(session, monkeypatch, outbox)
    assert confirm(session, outbox, ack=2)
    follow_up = visitor_decision(session, open_event("5234567890@lid", content="Outra pergunta"))
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert follow_up.response_introduction_included is False
    assert session.scalar(select(FirstContactPresentationRow)).state == "CONFIRMED"


@pytest.mark.parametrize("eligibility_mode", ["OPEN_INBOUND", "INDIVIDUAL"])
def test_generic_second_inbound_recovers_undispatched_introduction(
    session, monkeypatch, eligibility_mode,
):
    captured = (enabled_open_inbound(session, monkeypatch) if eligibility_mode == "OPEN_INBOUND"
                else enabled(session, monkeypatch))
    if eligibility_mode == "INDIVIDUAL":
        monkeypatch.setattr(settings, "first_contact_eligibility_mode", "INDIVIDUAL")
    monkeypatch.setattr(autonomy, "automation_denial_reason", lambda *_args, **_kwargs: "OWNER_AUTOMATION_PAUSED")
    first = visitor_decision(session, event())
    first_evaluation = autonomy.evaluate_and_route(session, first)
    session.commit()
    assert first.response_introduction_included is True
    assert first.proposed_response.startswith("[Andy • IA] Olá! Sou a Andy")
    assert first.status == "DRY_RUN" and not first_evaluation.automatic_execution_allowed
    assert not session.scalar(select(OutboxMessageRow))
    presentation = session.scalar(select(FirstContactPresentationRow))
    original_attempt_id = presentation.current_attempt_id
    assert presentation.state == "RESERVED"

    monkeypatch.setattr(autonomy, "automation_denial_reason", lambda *_args, **_kwargs: None)
    second = visitor_decision(session, event(content="Explique fibra de forma simples."))
    assert captured[-1].first_contact["mode"] == "INTRODUCE"
    assert second.response_introduction_included is True
    assert second.proposed_response.startswith("[Andy • IA] Olá! Sou a Andy, uma assistente virtual")
    assert session.get(FirstContactAttemptRow, original_attempt_id).state == "FAILED"
    assert presentation.generation == 2
    outbox = enqueue(session, second)
    send(session, monkeypatch, outbox)
    assert not confirm(session, outbox, ack=1)
    assert presentation.state == "SENT" and presentation.confirmed_at is None
    assert confirm(session, outbox, ack=2)
    third = visitor_decision(session, event(content="Mais uma dúvida."))
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert third.response_introduction_included is False
    assert third.proposed_response.startswith("[Andy • IA]")
    assert "Olá! Sou a Andy" not in third.proposed_response
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 2


@pytest.mark.parametrize("change", ["missing_identity", "other_tenant", "other_account", "group", "binding", "owner"])
def test_open_inbound_still_requires_proven_unbound_direct_scope(session, monkeypatch, change):
    captured = enabled_open_inbound(session, monkeypatch)
    peer = "3234567890@lid"
    incoming = open_event(peer)
    if change == "missing_identity":
        incoming.metadata.pop("first_contact_identity")
    elif change == "other_tenant":
        monkeypatch.setattr(settings, "first_contact_tenant_id", "synthetic-other-tenant")
    elif change == "other_account":
        incoming.metadata["source_account"] = "synthetic-other-account"
    elif change == "group":
        incoming.metadata["is_group"] = True
    elif change in {"binding", "owner"}:
        role = "owner" if change == "owner" else "bound_contact"
        upsert_actor_binding(session, "wwebjs", peer, f"synthetic-{role}", role,
                             metadata={"owner": change == "owner", "audience": role})
        session.commit()
    visitor_decision(session, incoming)
    assert not session.scalar(select(FirstContactPresentationRow))
    assert all(context.first_contact is None for context in captured)


def test_open_inbound_off_has_no_presentation_side_effect(session, monkeypatch):
    captured, _, _ = setup_guarded_visitor(session, monkeypatch)
    monkeypatch.setattr(settings, "first_contact_enabled", False)
    monkeypatch.setattr(settings, "first_contact_eligibility_mode", "OPEN_INBOUND")
    monkeypatch.setattr(settings, "first_contact_tenant_id", DEFAULT_TENANT_ID)
    monkeypatch.setattr(settings, "first_contact_source_account", "default")
    visitor_decision(session, open_event("4234567890@lid"))
    assert not session.scalar(select(FirstContactPresentationRow))
    assert all(context.first_contact is None for context in captured)


def test_agent_failure_before_effect_permits_only_new_event(session, monkeypatch):
    enabled(session, monkeypatch)
    original = pipeline.run_andy

    def failure(_):
        raise AndyAgentError("SYNTHETIC_FAILURE")

    monkeypatch.setattr(pipeline, "run_andy", failure)
    initial_event = event()
    failed = visitor_decision(session, initial_event)
    session.commit()
    assert failed.proposed_response is None
    state = session.scalar(select(FirstContactPresentationRow))
    assert state.state == "FAILED" and state.confirmed_at is None
    duplicate = services.receive_normalized_inbound_event(session, initial_event)
    assert duplicate["id"] == failed.interaction_id
    assert pipeline.process_agent_decision(session, failed.event_id).id == failed.id
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 1
    monkeypatch.setattr(pipeline, "run_andy", original)
    newer = visitor_decision(session, event())
    assert newer.response_introduction_included
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 2


def test_ambiguous_transport_has_no_automatic_resend_or_second_introduction(session, monkeypatch):
    captured = enabled(session, monkeypatch)
    first = visitor_decision(session, event())
    outbox = enqueue(session, first)
    calls = []

    def failure(row):
        calls.append(row.id)
        raise WwebjsOutboundError("SYNTHETIC_TIMEOUT")

    monkeypatch.setattr(services.local_transport_outbound, "dispatch_outbox", failure)
    assert services.process_outbox(session, "synthetic-fc-worker") == 1
    assert outbox.status == "AMBIGUOUS"
    state = session.scalar(select(FirstContactPresentationRow))
    assert state.state == "AMBIGUOUS" and state.confirmed_at is None
    assert services.process_outbox(session, "synthetic-fc-worker") == 0
    second = visitor_decision(session, event())
    assert second.response_introduction_included is False
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert calls == [outbox.id]
    assert confirm(session, outbox, 3)  # Late authenticated device ACK reconciles the same effect.
    assert not confirm(session, outbox, 1)
    assert session.scalar(select(FirstContactAttemptRow)).ack == 3


def test_known_last_mile_denial_does_not_complete_presentation(session, monkeypatch):
    enabled(session, monkeypatch)
    first = visitor_decision(session, event())
    outbox = enqueue(session, first)
    monkeypatch.setattr(services, "execution_gate", lambda *_args, **_kwargs: (
        "BLOCKED", "SYNTHETIC_GATE_DENIED", None,
    ))
    monkeypatch.setattr(
        services.local_transport_outbound, "dispatch_outbox",
        lambda *_: pytest.fail("denied first contact must not reach Transport"),
    )
    assert services.process_outbox(session, "synthetic-fc-worker") == 1
    assert outbox.status == "BLOCKED"
    state = session.scalar(select(FirstContactPresentationRow))
    assert state.state == "FAILED" and state.confirmed_at is None


@pytest.mark.parametrize("invalid", ["missing", "second_lid", "wrong_canonical", "unproved_pn"])
def test_unproven_identity_preserves_conversation_without_durable_inauguration(
    session, monkeypatch, invalid,
):
    captured = enabled(session, monkeypatch)
    incoming = event()
    if invalid == "missing":
        incoming.metadata.pop("first_contact_identity")
    elif invalid == "second_lid":
        incoming.metadata["peer_identifiers"].append("5500000000099@lid")
    elif invalid == "wrong_canonical":
        incoming.metadata["first_contact_identity"]["canonical_peer_id"] = "5500000000099@lid"
    else:
        incoming = event(peer="5500000000027@c.us", proof=False)
    decision = visitor_decision(session, incoming)
    assert decision.proposed_response == "[Andy • IA] Fibra óptica transmite dados por luz."
    assert decision.response_introduction_included is None
    assert captured[-1].first_contact is None
    assert not session.scalar(select(FirstContactPresentationRow))
    assert not session.scalar(select(ActorBindingRow).where(
        ActorBindingRow.external_actor_id.in_([PEER, "5500000000027@c.us"]),
    ))


def test_proven_pn_and_lid_share_presentation_but_do_not_create_binding(session, monkeypatch):
    captured = enabled(session, monkeypatch)
    first = visitor_decision(session, event())
    outbox = enqueue(session, first)
    send(session, monkeypatch, outbox)
    assert confirm(session, outbox)
    later = visitor_decision(session, event(peer="5500000000027@c.us"))
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert later.response_introduction_included is False
    assert session.scalar(select(func.count()).select_from(FirstContactPresentationRow)) == 1
    assert not session.scalar(select(ActorBindingRow).where(
        ActorBindingRow.external_actor_id.in_([PEER, "5500000000027@c.us"]),
    ))


def test_duplicate_event_and_pending_reservation_do_not_generate_two_introductions(
    session, monkeypatch,
):
    captured = enabled(session, monkeypatch)
    incoming = event()
    first = visitor_decision(session, incoming)
    session.commit()
    duplicate = services.receive_normalized_inbound_event(session, incoming)
    assert duplicate["id"] == first.interaction_id
    assert pipeline.process_agent_decision(session, first.event_id).id == first.id
    second = visitor_decision(session, event())
    assert len(captured) == 2
    assert captured[-1].first_contact["mode"] == "CONTINUE"
    assert second.response_introduction_included is False
    assert session.scalar(select(func.count()).select_from(FirstContactAttemptRow)) == 1


@pytest.mark.parametrize("exclusion", ["group", "other_tenant"])
def test_first_contact_does_not_expand_group_or_tenant_scope(session, monkeypatch, exclusion):
    captured = enabled(session, monkeypatch)
    incoming = event()
    if exclusion == "group":
        incoming.metadata["is_group"] = True
    else:
        monkeypatch.setattr(settings, "first_contact_tenant_id", "synthetic-other-tenant")
    visitor_decision(session, incoming)
    assert not session.scalar(select(FirstContactPresentationRow))
    assert all(context.first_contact is None for context in captured)


def test_reservation_does_not_hold_new_row_lock_during_agent(session, monkeypatch):
    enabled(session, monkeypatch)
    original = pipeline.run_andy

    def agent(context):
        assert not session.in_transaction()
        return original(context)

    monkeypatch.setattr(pipeline, "run_andy", agent)
    visitor_decision(session, event())


def test_flag_off_preserves_existing_response_and_prompt_payload(session, monkeypatch):
    captured, _, _ = setup_guarded_visitor(session, monkeypatch)
    monkeypatch.setattr(settings, "first_contact_enabled", False)
    decision = visitor_decision(session, event())
    assert captured[0].first_contact is None
    assert "first_contact" not in captured[0].prompt_payload()
    assert decision.proposed_response == "[Andy • IA] Fibra óptica transmite dados por luz."
    assert not session.scalar(select(FirstContactPresentationRow))


@pytest.mark.parametrize("text", [
    "Sou a Andy, assistente virtual.", "Eu me chamo Andy, uma assistente virtual.",
    "Meu nome é Andy.", "Aqui é a Andy.", "I'm Andy.", "My name is Andy.",
    "Me llamo Andy.", "Sou uma IA.",
])
def test_unrequested_reintroduction_is_held(text):
    with pytest.raises(AndyAgentError, match="UNSOLICITED_INTRODUCTION"):
        render_public_identity_response(
            text, mode="CONTINUE", current_message="Explique fibra.",
            locale="pt-BR",
        )


def test_public_agent_uses_shared_instructions_without_private_owner_name(monkeypatch):
    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        Agent=lambda **kwargs: kwargs,
        AgentOutputSchema=lambda *_args, **_kwargs: object(),
    ))
    public = build_andy_agent(public_identity=True)
    assert "alex" not in public["instructions"].casefold()
    assert "assistente virtual nesta conta" in public["instructions"]
    assert "Alex" in build_andy_agent()["instructions"]


def test_enable_requires_explicit_tenant_and_account():
    with pytest.raises(ValueError, match="FIRST_CONTACT_TENANT_AND_ACCOUNT_REQUIRED"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False, first_contact_enabled=True)


def test_enable_requires_canonical_canary_hash():
    with pytest.raises(ValueError, match="FIRST_CONTACT_CANARY_PEER_SHA256_REQUIRED"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                 first_contact_enabled=True, first_contact_tenant_id="tenant-test",
                 first_contact_source_account="account-test")
    with pytest.raises(ValueError, match="FIRST_CONTACT_CANARY_PEER_SHA256_REQUIRED"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                 first_contact_enabled=True, first_contact_tenant_id="tenant-test",
                 first_contact_source_account="account-test",
                 first_contact_canary_peer_sha256="not-a-canonical-hash")


def test_eligibility_mode_rejects_unknown_and_contradictory_configuration():
    with pytest.raises(ValueError, match="FIRST_CONTACT_ELIGIBILITY_MODE_INVALID"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                 first_contact_eligibility_mode="OPEN_ANYTHING")
    with pytest.raises(ValueError, match="FIRST_CONTACT_OPEN_MODE_PEER_CONFLICT"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                 first_contact_eligibility_mode="OPEN_INBOUND",
                 first_contact_canary_peer_sha256="a" * 64)


def test_open_mode_requires_tenant_and_account_but_not_a_peer_digest():
    candidate = Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                         first_contact_enabled=True, first_contact_eligibility_mode="OPEN_INBOUND",
                         first_contact_tenant_id="tenant-test", first_contact_source_account="account-test")
    assert candidate.first_contact_canary_peer_sha256 is None
    with pytest.raises(ValueError, match="FIRST_CONTACT_TENANT_AND_ACCOUNT_REQUIRED"):
        Settings(_env_file=None, app_env="test", admin_auth_enabled=False,
                 first_contact_enabled=True, first_contact_eligibility_mode="OPEN_INBOUND",
                 first_contact_tenant_id="tenant-test")


def test_other_verified_peer_keeps_ordinary_visitor_path(session, monkeypatch):
    captured = enabled(session, monkeypatch)
    incoming = event(peer="5500000000099@lid")
    incoming.metadata["conversation_key"] = "wwebjs:" + incoming.actor_id
    incoming.metadata["peer_identifiers"] = [incoming.actor_id]
    incoming.metadata["first_contact_identity"]["canonical_peer_id"] = incoming.actor_id
    decision = visitor_decision(session, incoming)
    assert decision.proposed_response == "[Andy • IA] Fibra óptica transmite dados por luz."
    assert decision.response_introduction_included is None
    assert captured[-1].first_contact is None
    assert not session.scalar(select(FirstContactPresentationRow))


def test_receipt_poll_rotates_pending_attempts_without_releasing_unknown_effects(session, monkeypatch):
    enabled(session, monkeypatch)
    outboxes = []
    for index in range(3):
        incoming = event(peer=f"55000000001{index:02d}@lid")
        incoming.metadata["conversation_key"] = "wwebjs:" + incoming.actor_id
        incoming.metadata["peer_identifiers"] = [incoming.actor_id]
        incoming.metadata["first_contact_identity"]["canonical_peer_id"] = incoming.actor_id
        incoming.metadata["first_contact_identity"]["provenance"] = "DIRECTIONAL_LID"
        monkeypatch.setattr(settings, "first_contact_canary_peer_sha256",
                            hashlib.sha256(incoming.actor_id.encode()).hexdigest())
        outboxes.append(enqueue(session, visitor_decision(session, incoming)))
    probed = []

    def probe(scope):
        assert not session.in_transaction()
        probed.append(scope["outbox_id"])
        return None

    adapter = SimpleNamespace(probe=probe)
    assert reconcile_presentations(session, adapter=adapter, limit=2) == 0
    assert reconcile_presentations(session, adapter=adapter, limit=2) == 0
    assert set(probed) == {row.id for row in outboxes}
    assert all(row.state == "QUEUED" for row in session.scalars(select(FirstContactAttemptRow)))


def test_proven_alias_with_owner_binding_denies_intro_without_removing_public_reply(
    session, monkeypatch,
):
    captured = enabled(session, monkeypatch)
    phone = "5500000000027@c.us"
    upsert_actor_binding(session, "wwebjs", phone, "synthetic-owner", "owner",
                         metadata={"owner": True, "audience": "owner"})
    session.commit()
    incoming = event()
    incoming.metadata["peer_identifiers"] = [PEER, phone]
    incoming.metadata["first_contact_identity"]["provenance"] = "PROVIDER_LID_PN_MAPPING"
    decision = visitor_decision(session, incoming)
    assert decision.proposed_response.startswith("[Andy • IA] Fibra")
    assert decision.response_introduction_included is False
    assert captured[-1].first_contact["mode"] == "UNAVAILABLE"
    assert captured[-1].represented_subject is None
    assert not session.scalar(select(FirstContactPresentationRow))
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed


@pytest.mark.parametrize("tamper", ["text", "recipient", "metadata"])
def test_tampered_presentation_is_quarantined_before_transport_and_worker_keeps_running(
    session, monkeypatch, tamper,
):
    enabled(session, monkeypatch)
    outbox = enqueue(session, visitor_decision(session, event()))
    payload = dict(outbox.payload)
    if tamper == "text":
        payload["text"] = "synthetic changed text"
    elif tamper == "recipient":
        payload["external_actor_id"] = "5500000000099@lid"
    else:
        payload["first_contact"] = {**payload["first_contact"], "attempt_id": "wrong-attempt"}
    outbox.payload = payload
    session.commit()
    monkeypatch.setattr(
        services.local_transport_outbound, "dispatch_outbox",
        lambda *_: pytest.fail("tampered presentation must not reach Transport"),
    )
    assert services.process_outbox(session, "synthetic-fc-worker") == 0
    assert outbox.status == "BLOCKED"
    assert session.scalar(select(FirstContactPresentationRow)).confirmed_at is None
    assert services.process_outbox(session, "synthetic-fc-worker") == 0


def test_stale_attempt_during_receipt_poll_does_not_abort_the_batch(session, monkeypatch):
    enabled(session, monkeypatch)
    original_observe = first_contact_runtime.observe_outbox
    outboxes = []
    for index in range(2):
        incoming = event(peer=f"55000000002{index:02d}@lid")
        incoming.metadata["conversation_key"] = "wwebjs:" + incoming.actor_id
        incoming.metadata["peer_identifiers"] = [incoming.actor_id]
        incoming.metadata["first_contact_identity"]["canonical_peer_id"] = incoming.actor_id
        incoming.metadata["first_contact_identity"]["provenance"] = "DIRECTIONAL_LID"
        monkeypatch.setattr(settings, "first_contact_canary_peer_sha256",
                            hashlib.sha256(incoming.actor_id.encode()).hexdigest())
        outboxes.append(enqueue(session, visitor_decision(session, incoming)))

    def raced(db, outbox_id):
        if outbox_id == outboxes[0].id:
            raise fc.FirstContactConflict("FIRST_CONTACT_STALE_ATTEMPT")
        return original_observe(db, outbox_id)

    monkeypatch.setattr(first_contact_runtime, "observe_outbox", raced)
    probed = []
    adapter = SimpleNamespace(
        probe=lambda scope: probed.append(scope["outbox_id"]),
    )
    assert reconcile_presentations(session, adapter=adapter, limit=1) == 0
    assert probed == []
    assert reconcile_presentations(session, adapter=adapter, limit=1) == 0
    assert probed == [outboxes[1].id]


@pytest.mark.parametrize("edited", [False, True])
def test_human_review_keeps_authority_and_changed_intro_is_quarantined_per_item(
    session, monkeypatch, edited,
):
    enabled(session, monkeypatch)
    policy = session.get(PolicyRow, "desconhecido")
    config = deepcopy(session.get(PolicyVersionRow, policy.current_version_id).config)
    config["conversational_execution"]["execution_mode"] = "REQUIRES_APPROVAL"
    ensure_policy_version(session, policy, config, "synthetic first contact review")
    session.commit()
    decision = visitor_decision(session, event())
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not evaluation.automatic_execution_allowed
    assert evaluation.effective_mode == "REQUIRES_APPROVAL"
    review = session.scalar(select(AgentResponseReviewRow).where(
        AgentResponseReviewRow.agent_decision_id == decision.id,
    ))
    assert review.status == "PENDING"
    assert not session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.causation_id == decision.id,
    ))
    if edited:
        response_review.edit_review(
            session, review.id, decision.proposed_response + " Synthetic approved edit.",
        )
    _, intent = response_review.approve_review(session, review.id, "synthetic-reviewer")
    execution.release_intent(session, intent.id, transport_ready=True)
    session.commit()
    assert intent.authorization_source == "HUMAN_REVIEW"
    count = execution.enqueue_ready_intents(session, transport_ready=True)
    session.commit()
    outbox = session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.causation_id == decision.id,
    ))
    state = session.scalar(select(FirstContactPresentationRow))
    if edited:
        assert count == 0
        assert intent.status == "BLOCKED" and outbox.status == "BLOCKED"
        assert intent.blocked_reason == "FIRST_CONTACT_EFFECT_LINK_DENIED"
        assert state.confirmed_at is None
        assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    else:
        assert count == 1 and outbox.status == "PENDING"
        assert state.state == "QUEUED"
        send(session, monkeypatch, outbox)
        assert confirm(session, outbox)


def test_ptbr_inaugural_message_is_impactful_without_duplicate_generic_reply():
    response = render_public_identity_response(
        'Oi! Como posso ajudar?', mode='INTRODUCE', current_message='Oi Leo', locale='pt-BR',
    )
    assert response.startswith('[Andy • IA] Olá! Sou a Andy')
    assert 'com voz' in response and 'inteligência artificial' in response
    assert response.endswith('Me conta: o que te trouxe até esta conversa?')
    assert 'Oi! Como posso ajudar?' not in response
    assert 'Alex' not in response
    meaningful = render_public_identity_response(
        'Posso explicar como funciona a tecnologia.', mode='INTRODUCE',
        current_message='Me explica a tecnologia', locale='pt-BR',
    )
    assert meaningful.endswith('Posso explicar como funciona a tecnologia.')
    assert meaningful.count('Olá! Sou a Andy') == 1


def test_authenticated_reconciliation_recovers_ambiguous_text_and_allows_tts(
    session, monkeypatch, tmp_path,
):
    enabled(session, monkeypatch)
    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_auto_send", True)
    monkeypatch.setattr(settings, "tts_supplemental_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    decision = visitor_decision(session, event(content="Olá"))
    text = enqueue(session, decision)
    intent = session.get(AgentExecutionIntentRow, text.execution_intent_id)
    text.status = "AMBIGUOUS"
    text.last_error = "TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS"
    intent.status = "BLOCKED"
    intent.blocked_reason = text.last_error
    first_contact_runtime.observe_presentation_outbox(session, text)
    session.commit()
    assert session.scalar(select(FirstContactPresentationRow)).state == "AMBIGUOUS"
    proof = signed_proof(session, SimpleNamespace(outbox=text), ack=3)
    adapter = SimpleNamespace(probe=lambda expected: proof)
    assert reconcile_presentations(session, adapter=adapter) == 1
    assert text.status == "DONE" and text.completed_at is not None
    assert text.last_error is None
    assert intent.status == "SENT" and intent.blocked_reason is None
    assert session.scalar(select(FirstContactPresentationRow)).state == "CONFIRMED"
    fake = FakeTTS()
    assert process_tts_derivations(session, "tts", client=fake) == 1
    voice = session.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.idempotency_key == f"execution:{intent.id}:audio"
    ))
    assert voice is not None and voice.action_type == "agent_execution_voice"
    assert voice.payload["parent_outbox_id"] == text.id
    assert fake.calls == 1
    validate_voice_outbox(session, voice, intent)
    calls = []

    def dispatch_only_audio(item):
        calls.append(item.id)
        return SimpleNamespace(status="sent", response={"message_reference": "synthetic-audio-reference"})

    monkeypatch.setattr(services.local_transport_outbound, "dispatch_outbox", dispatch_only_audio)
    services.process_outbox(session, "synthetic-recovery-audio")
    assert calls == [voice.id]
    assert voice.status == "DONE" and text.status == "DONE"
    assert intent.status == "SENT"
    assert reconcile_presentations(session, adapter=adapter) == 0


def test_unverified_transport_failure_never_recovers_primary_outbox(session, monkeypatch):
    enabled(session, monkeypatch)
    decision = visitor_decision(session, event())
    text = enqueue(session, decision)
    intent = session.get(AgentExecutionIntentRow, text.execution_intent_id)
    text.status = "AMBIGUOUS"
    text.last_error = "TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS"
    intent.status = "BLOCKED"
    intent.blocked_reason = text.last_error
    first_contact_runtime.observe_presentation_outbox(session, text)
    session.commit()
    assert reconcile_presentations(session, adapter=SimpleNamespace(probe=lambda expected: None)) == 0
    assert text.status == "AMBIGUOUS" and intent.status == "BLOCKED"
    assert session.scalar(select(FirstContactPresentationRow)).state == "AMBIGUOUS"
