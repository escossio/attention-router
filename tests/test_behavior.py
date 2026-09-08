import json
from pathlib import Path
from types import SimpleNamespace

from attention_router.domain.behavior import (
    behavior_profile,
    explicit_intent,
    extract_conversation_slots,
    promise_guard,
    render_response,
    variant_capacity,
)


PROFILE = json.loads((Path(__file__).parents[1] / "config/andy_behavior_profile.json").read_text())


def result(decision_type="INSUFFICIENT_CONTEXT", action="request_information", escalation=False, recorded=False):
    return SimpleNamespace(
        decision_type=decision_type,
        recommended_action=action,
        escalation_required=escalation,
        escalation_recorded=recorded,
    )


def test_core_request_family_has_compositional_capacity():
    assert variant_capacity(PROFILE, "REQUEST_CONTEXT") >= 20


def test_first_contact_introduces_andy_and_follow_up_does_not():
    first = render_response(result(), PROFILE, audience="family_core")
    follow_up = render_response(result(), PROFILE, audience="family_core", introduced=True)
    assert first is not None and first.introduction_included is True
    assert "Andy" in first.text
    assert follow_up is not None and follow_up.introduction_included is False
    assert "Andy" not in follow_up.text


def test_recent_variants_are_avoided():
    first = render_response(result(), PROFILE, audience="unknown")
    second = render_response(result(), PROFILE, audience="unknown", recent_variant_ids={first.variant_id})
    assert first.variant_id != second.variant_id


def test_variation_produces_ten_unique_candidates():
    recent = set()
    variants = set()
    for _ in range(10):
        candidate = render_response(result(), PROFILE, audience="family_core", recent_variant_ids=recent)
        variants.add(candidate.variant_id)
        recent.add(candidate.variant_id)
    assert len(variants) == 10


def test_decision_semantics_are_preserved():
    assert render_response(result("DO_NOT_RESPOND", "observe"), PROFILE, audience="unknown") is None
    candidate = render_response(result("INSUFFICIENT_CONTEXT", "request_information"), PROFILE, audience="unknown")
    assert candidate.message_family == "REQUEST_CONTEXT"
    assert candidate.promise_check == "NO_UNSUPPORTED_PROMISE"


def test_persistent_preferred_name_can_be_used_naturally():
    candidate = render_response(
        result(), PROFILE, audience="unknown", inbound_text="Bom dia.", introduced=False,
        memory_context={"preferred_name": "André"},
    )
    assert candidate is not None
    assert "André" in candidate.text
    assert candidate.spoken_text


def test_escalation_promise_is_marked_supported():
    candidate = render_response(
        result("ESCALATE", "soft_ping", escalation=True, recorded=True), PROFILE, audience="family_core"
    )
    assert candidate.promise_check == "SUPPORTED_ESCALATION"


def test_escalation_without_recorded_intent_does_not_promise_action():
    candidate = render_response(result("ESCALATE", "soft_ping", escalation=True), PROFILE, audience="family_core")
    assert candidate.promise_check == "NO_UNSUPPORTED_PROMISE"
    assert "Vou " not in candidate.text


def test_audience_styles_change_request_without_changing_family():
    candidate = render_response(result(), PROFILE, audience="recruiters")
    assert candidate.message_family == "REQUEST_CONTEXT"
    assert "empresa" in candidate.text.casefold() or "vaga" in candidate.text.casefold()


def test_direct_identity_question_is_answered_without_generic_follow_up():
    candidate = render_response(result(), PROFILE, audience="unknown", inbound_text="Quem é?")
    assert candidate is not None
    assert "Andy" in candidate.text
    assert "assunto" not in candidate.text.casefold()


def test_name_questions_use_identity_branch_and_not_callback_prompt():
    for question in ("Qual é o seu nome?", "Quem fala?", "Como você se chama?"):
        assert explicit_intent(question) == "IDENTITY_QUESTION"
        candidate = render_response(result(), PROFILE, audience="unknown", inbound_text=question)
        assert "andy" in candidate.text.casefold()
        assert "assistente virtual" in candidate.text.casefold()
        assert "assunto" not in candidate.text.casefold()
        assert candidate.message_family == "IDENTITY"
        assert candidate.variant_id.startswith("identity-")


def test_assistant_nature_questions_are_answered_transparently():
    questions = (
        "Você é um robô?", "Você é uma IA?", "Você é humana?", "Você é uma pessoa?",
        "Estou falando com uma máquina?", "Você responde automaticamente?",
        "Você é assistente dele?", "Quem é você de verdade?", "Você é uma assistente virtual?",
    )
    for question in questions:
        assert explicit_intent(question) == "ASSISTANT_NATURE_QUESTION"
        candidate = render_response(result(), PROFILE, audience="unknown", inbound_text=question)
        assert candidate is not None
        assert "assistente virtual" in candidate.text.casefold()
        assert "alex" in candidate.text.casefold()
        assert "robô" not in candidate.text.casefold() or "não uma pessoa" in candidate.text.casefold()
        assert "worker" not in candidate.text.casefold()


def test_alex_identity_question_denies_impersonation():
    candidate = render_response(result(), PROFILE, audience="unknown", inbound_text="É o Alex?")
    assert explicit_intent("É o Alex?") == "OWNER_IDENTITY_QUESTION"
    assert "não" in candidate.text.casefold()
    assert "andy" in candidate.text.casefold()
    assert "assistente virtual" in candidate.text.casefold()


def test_functional_identity_question_is_preserved():
    candidate = render_response(result(), PROFILE, audience="unknown", inbound_text="Com quem estou falando?")
    assert explicit_intent("Com quem estou falando?") == "IDENTITY_QUESTION"
    assert "andy" in candidate.text.casefold()
    assert "assistente" in candidate.text.casefold()


def test_assistant_nature_question_respects_introduction_state_multiturn():
    first = render_response(result(), PROFILE, audience="unknown", inbound_text="Oi")
    follow_up = render_response(
        result(), PROFILE, audience="unknown", introduced=True, inbound_text="Você é um robô?"
    )
    assert first.introduction_included is True
    assert follow_up.introduction_included is False
    assert "assistente virtual" in follow_up.text.casefold()
    assert "worker" not in follow_up.text.casefold()
    assert "policy" not in follow_up.text.casefold()
    assert "pipeline" not in follow_up.text.casefold()


def test_relationship_uncertainty_does_not_invent_what_alex_feels():
    candidate = render_response(result(), PROFILE, audience="relationship", inbound_text="Ele está bem?")
    assert candidate is not None
    assert "não tenho informação suficiente" in candidate.text.casefold()
    assert "ele está bem" not in candidate.text.casefold()


def test_same_intent_has_audience_specific_realizations():
    outputs = {
        audience: render_response(result(), PROFILE, audience=audience, inbound_text="Preciso falar com Alex.").text
        for audience in ("family_core", "recruiters", "business_clients", "unknown", "friends", "relationship")
    }
    assert "empresa" in outputs["recruiters"].casefold() or "vaga" in outputs["recruiters"].casefold()
    assert "assunto" in outputs["business_clients"].casefold() or "serviço" in outputs["business_clients"].casefold()
    assert len(set(outputs.values())) >= 4


def test_multiturn_context_uses_follow_up_state_and_specific_slot():
    first = render_response(result(), PROFILE, audience="unknown", inbound_text="Oi")
    follow_up = render_response(
        result(),
        PROFILE,
        audience="recruiters",
        introduced=True,
        inbound_text="Pode ser amanhã de manhã.",
        missing_information=["interview_schedule"],
    )
    assert first.introduction_included is True
    assert follow_up.introduction_included is False
    assert "dia" in follow_up.text.casefold() or "horário" in follow_up.text.casefold()


def test_unknown_location_question_does_not_disclose_or_invent():
    candidate = render_response(result(), PROFILE, audience="unknown", inbound_text="Ele está em casa?")
    assert candidate is not None
    assert "localização" in candidate.text.casefold() or "localizacao" in candidate.text.casefold()
    assert "sim" not in candidate.text.casefold()


def test_promise_guard_rejects_unbacked_registration_promise():
    assert promise_guard("Posso deixar sua mensagem registrada para ele.") == "UNSUPPORTED_PROMISE"


def test_behavior_profile_is_fail_closed_when_production_disabled():
    assert behavior_profile({"behavior": {"production_enabled": False}}) is None


def test_callback_request_is_acknowledged_before_missing_context_question():
    candidate = render_response(result(), PROFILE, audience="family_core", inbound_text="Pede para ele me ligar.")
    assert explicit_intent("Pede para ele me ligar.") == "CALLBACK_REQUEST"
    assert "ligue" in candidate.text.casefold() or "ligue" in candidate.text.casefold() or "ligar" in candidate.text.casefold()
    assert "o que você queria falar" not in candidate.text.casefold()


def test_confirmation_question_is_answered_with_uncertainty_first():
    candidate = render_response(result(), PROFILE, audience="family_core", inbound_text="Ele pediu para eu falar com você?")
    assert "não tenho essa confirmação" in candidate.text.casefold()


def test_future_notification_request_does_not_make_unbacked_promise():
    candidate = render_response(result(), PROFILE, audience="friends", inbound_text="Me avisa quando ele aparecer.")
    assert "não consigo prometer" in candidate.text.casefold()
    assert candidate.promise_check == "NO_UNSUPPORTED_PROMISE"


def test_slots_carry_company_interview_date_and_time_without_reasking_known_values():
    slots = extract_conversation_slots("Sou da empresa X.")
    slots = extract_conversation_slots("Quero marcar uma entrevista.", slots)
    slots = extract_conversation_slots("Pode amanhã às 10h?", slots)
    assert slots.known["company"].startswith("X")
    assert slots.known["subject"] == "interview"
    assert slots.known["proposed_date"] == "tomorrow"
    assert slots.known["proposed_time"] == "10:00"
    candidate = render_response(
        result(),
        PROFILE,
        audience="recruiters",
        inbound_text="Pode amanhã às 10h?",
        known_slots=slots,
    )
    assert "vaga" in candidate.text.casefold()
    assert "qual dia" not in candidate.text.casefold()
    assert "qual horário" not in candidate.text.casefold()


def test_known_recruiter_company_is_not_asked_again():
    slots = extract_conversation_slots("Sou da empresa X.")
    candidate = render_response(
        result(),
        PROFILE,
        audience="recruiters",
        inbound_text="Sou da empresa X.",
        known_slots=slots,
    )
    assert "de qual empresa" not in candidate.text.casefold()
    assert "vaga" in candidate.text.casefold()


def test_scheduling_proposal_is_explicit_intent():
    assert explicit_intent("Pode amanhã às 10h?") == "SCHEDULING_PROPOSAL"


def test_scheduling_proposal_does_not_confirm_availability():
    slots = extract_conversation_slots("Sou da empresa X.")
    slots = extract_conversation_slots("Quero marcar uma entrevista.", slots)
    slots = extract_conversation_slots("Pode amanhã às 10h?", slots)
    candidate = render_response(
        result(), PROFILE, audience="recruiters", inbound_text="Pode amanhã às 10h?", known_slots=slots
    )
    assert slots.schedule_status == "PROPOSED"
    assert "sugestão" in candidate.text.casefold()
    assert "não consigo confirmar" in candidate.text.casefold()
    assert "qual é a vaga" in candidate.text.casefold()
    assert "ficou para" not in candidate.text.casefold()


def test_completed_recruiter_context_keeps_schedule_as_proposal():
    slots = extract_conversation_slots("Sou da empresa X.")
    slots = extract_conversation_slots("Quero marcar uma entrevista.", slots)
    slots = extract_conversation_slots("Pode amanhã às 10h?", slots)
    slots = extract_conversation_slots("É para vaga de SRE.", slots)
    candidate = render_response(
        result(), PROFILE, audience="recruiters", inbound_text="É para vaga de SRE.", known_slots=slots
    )
    assert slots.schedule_status == "PROPOSED"
    assert "sugestão" in candidate.text.casefold()
    assert "não consigo confirmar" in candidate.text.casefold()
    assert "ficou para" not in candidate.text.casefold()


def test_schedule_confirmation_question_is_not_accepted_implicitly():
    candidate = render_response(result(), PROFILE, audience="recruiters", inbound_text="Está confirmado?")
    assert explicit_intent("Está confirmado?") == "AMBIGUOUS_CONFIRMATION_QUESTION"
    assert "qual informação" in candidate.text.casefold()
    assert "agenda" not in candidate.text.casefold()


def test_schedule_confirmation_statement_does_not_mark_schedule_confirmed():
    slots = extract_conversation_slots("Pode amanhã às 10h?")
    candidate = render_response(result(), PROFILE, audience="recruiters", inbound_text="Então ficou amanhã às 10?", known_slots=slots)
    assert explicit_intent("Então ficou amanhã às 10?") == "SCHEDULE_CONFIRMATION_QUESTION"
    assert slots.schedule_status == "PROPOSED"
    assert "não está confirmada" in candidate.text.casefold()


def test_can_mark_does_not_claim_to_schedule_without_capability():
    candidate = render_response(result(), PROFILE, audience="recruiters", inbound_text="Pode marcar.")
    assert explicit_intent("Pode marcar.") == "SCHEDULE_ACTION_REQUEST"
    assert "não consigo marcar" in candidate.text.casefold()
    assert "marcado" not in candidate.text.casefold()


def test_confirmation_offer_does_not_claim_unsupported_forwarding():
    candidate = render_response(result(), PROFILE, audience="family_core", inbound_text="Ele pediu para eu falar com você?")
    assert "não tenho essa confirmação" in candidate.text.casefold()
    assert "encaminho" not in candidate.text.casefold()


def test_read_receipt_question_is_treated_as_privacy_uncertainty():
    candidate = render_response(result(), PROFILE, audience="unknown", inbound_text="Ele viu minha mensagem?")
    assert explicit_intent("Ele viu minha mensagem?") == "MESSAGE_READ_STATUS_QUESTION"
    assert "não consigo confirmar se o alex já viu" in candidate.text.casefold()
    assert "localização" not in candidate.text.casefold()


def test_schedule_action_request_is_not_confirmation():
    candidate = render_response(result(), PROFILE, audience="recruiters", inbound_text="Pode marcar.")
    assert explicit_intent("Pode marcar.") == "SCHEDULE_ACTION_REQUEST"
    assert "não consigo marcar" in candidate.text.casefold()
    assert "confirmado" not in candidate.text.casefold()


def test_confirmation_without_schedule_context_is_ambiguous():
    candidate = render_response(result(), PROFILE, audience="recruiters", inbound_text="Está confirmado?")
    assert explicit_intent("Está confirmado?") == "AMBIGUOUS_CONFIRMATION_QUESTION"
    assert "qual informação" in candidate.text.casefold()
    assert "agenda" not in candidate.text.casefold()


def test_confirmation_with_proposed_schedule_uses_schedule_context():
    slots = extract_conversation_slots("Pode amanhã às 10h?")
    candidate = render_response(
        result(), PROFILE, audience="recruiters", inbound_text="Está confirmado?", known_slots=slots
    )
    assert explicit_intent("Está confirmado?", slots) == "SCHEDULE_CONFIRMATION_QUESTION"
    assert slots.schedule_status == "PROPOSED"
    assert "não está confirmada" in candidate.text.casefold()


def test_multiturn_scheduling_does_not_repeat_introduction():
    slots = None
    introduced = False
    turns = ["Sou da empresa X.", "Quero marcar uma entrevista.", "Pode amanhã às 10h?", "É para vaga de SRE."]
    candidates = []
    for turn in turns:
        slots = extract_conversation_slots(turn, slots)
        candidate = render_response(
            result(), PROFILE, audience="recruiters", inbound_text=turn,
            known_slots=slots, introduced=introduced,
        )
        candidates.append(candidate)
        introduced = True
    assert candidates[0].introduction_included is True
    assert all(candidate.introduction_included is False for candidate in candidates[1:])
    assert all("andy" not in candidate.text.casefold() for candidate in candidates[1:])


def test_composition_does_not_repeat_the_same_identity_fact():
    slots = extract_conversation_slots("Preciso falar com Alex.")
    recent = set()
    for _ in range(8):
        candidate = render_response(
            result(), PROFILE, audience="family_core", inbound_text="Preciso falar com Alex.",
            known_slots=slots, recent_variant_ids=recent,
        )
        assert candidate.text.casefold().count("ajudando o alex") <= 1
        recent.add(candidate.variant_id)
