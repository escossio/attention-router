"""Config-driven linguistic realization for agent decisions."""

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
import re
from typing import Any


@dataclass(frozen=True)
class ResponseCandidate:
    message_family: str
    variant_id: str
    text: str
    spoken_text: str
    introduction_included: bool
    promise_check: str


@dataclass(frozen=True)
class ConversationSlots:
    known: dict[str, str]
    states: dict[str, str]
    schedule_status: str = "UNKNOWN"


def extract_conversation_slots(text: str, existing: ConversationSlots | None = None) -> ConversationSlots:
    known = dict(existing.known if existing else {})
    company = re.search(r"\bempresa\s+([\wÀ-ÿ-]+(?:\s+[\wÀ-ÿ-]+){0,2})", text, re.IGNORECASE)
    role = re.search(r"\bvaga\s+(?:de\s+)?([^,.!?]+)", text, re.IGNORECASE)
    clock = re.search(r"\b(?:às|as)\s+(\d{1,2})(?:h|:(\d{2}))?\b", text, re.IGNORECASE)
    if company:
        known["company"] = company.group(1).strip()
    if role:
        known["job_role"] = role.group(1).strip()
    if clock:
        known["proposed_time"] = f"{clock.group(1)}:{clock.group(2) or '00'}"
    if _text_has(text, "amanhã", "amanha"):
        known["proposed_date"] = "tomorrow"
    if _text_has(text, "me ligar", "me ligue", "ligar para mim"):
        known["requested_callback"] = "yes"
    if _text_has(text, "me avisa", "me avise", "avisar quando", "quando ele aparecer"):
        known["future_notification"] = "yes"
    if _text_has(text, "viu minha mensagem", "visualizou minha mensagem"):
        known["message_read_status_question"] = "yes"
    if _text_has(text, "entrevista", "vaga", "recrutamento"):
        known["subject"] = "interview"
    if _text_has(text, "tudo fora", "parou", "indisponível", "indisponivel"):
        known["impact"] = "service_disruption"
    if _text_has(text, "urgente", "agora", "não pode esperar", "nao pode esperar"):
        known["urgency"] = "urgent"
    if _text_has(text, "está bem", "esta bem", "chateado", "como ele está", "como ele esta"):
        known["status_question"] = "yes"
    if _text_has(text, "em casa", "onde ele", "localização", "localizacao"):
        known["location_question"] = "yes"
    if _text_has(text, "pediu para eu falar", "pediu pra eu falar"):
        known["confirmation_question"] = "yes"
    states = {key: "KNOWN" for key in known}
    schedule_status = existing.schedule_status if existing else "UNKNOWN"
    if "proposed_date" in known or "proposed_time" in known:
        schedule_status = "PROPOSED"
    return ConversationSlots(known=known, states=states, schedule_status=schedule_status)


def explicit_intent(text: str, known_slots: ConversationSlots | None = None) -> str:
    if _text_has(
        text,
        "você é um robô", "voce e um robo", "você é uma ia", "voce e uma ia",
        "você é humana", "voce e humana", "você é uma pessoa", "voce e uma pessoa",
        "estou falando com uma máquina", "estou falando com uma maquina",
        "você responde automaticamente", "voce responde automaticamente",
        "você é assistente dele", "voce e assistente dele", "quem é você de verdade",
        "quem e voce de verdade", "você é uma assistente virtual", "voce e uma assistente virtual",
        "é automática", "e automatica",
    ):
        return "ASSISTANT_NATURE_QUESTION"
    if _text_has(text, "é o alex", "e o alex", "estou falando com o alex"):
        return "OWNER_IDENTITY_QUESTION"
    if _text_has(
        text,
        "quem é", "quem e", "com quem estou falando", "quem fala",
        "qual é o seu nome", "qual e o seu nome", "como você se chama",
        "como voce se chama", "como se chama",
    ):
        return "IDENTITY_QUESTION"
    if _text_has(text, "está bem", "esta bem", "chateado", "como ele está", "como ele esta"):
        return "STATUS_QUESTION"
    if _text_has(text, "viu minha mensagem", "visualizou minha mensagem"):
        return "MESSAGE_READ_STATUS_QUESTION"
    if _text_has(text, "em casa", "onde ele", "localização", "localizacao"):
        return "LOCATION_QUESTION"
    if _text_has(text, "pediu para eu falar", "pediu pra eu falar"):
        return "CONFIRMATION_QUESTION"
    if _text_has(text, "pode marcar"):
        return "SCHEDULE_ACTION_REQUEST"
    if _text_has(text, "está confirmado", "esta confirmado"):
        if known_slots and known_slots.schedule_status == "PROPOSED":
            return "SCHEDULE_CONFIRMATION_QUESTION"
        return "AMBIGUOUS_CONFIRMATION_QUESTION"
    if _text_has(
        text,
        "está marcado",
        "esta marcado",
        "pode marcar",
        "ficou amanhã",
        "ficou amanha",
    ):
        return "SCHEDULE_CONFIRMATION_QUESTION"
    if _text_has(text, "me avisa", "me avise", "quando ele aparecer", "avisar quando"):
        return "FUTURE_NOTIFICATION_REQUEST"
    if _text_has(text, "me ligar", "me ligue", "ligar para mim"):
        return "CALLBACK_REQUEST"
    if _text_has(text, "amanhã", "amanha") and _text_has(text, "às", "as "):
        return "SCHEDULING_PROPOSAL"
    if _text_has(text, "entrevista", "vaga", "recrutamento"):
        return "SCHEDULING_PROPOSAL" if _text_has(text, "amanhã", "amanha", "às", "as ") else "INTERVIEW_REQUEST"
    if _text_has(text, "urgente", "agora", "não pode esperar", "nao pode esperar"):
        return "URGENCY"
    return "GENERAL_CONTEXT_REQUEST"


def response_objective(result: Any, text: str, audience: str | None, known_slots: ConversationSlots | None = None) -> str:
    intent = explicit_intent(text, known_slots)
    if intent in {"IDENTITY_QUESTION", "ASSISTANT_NATURE_QUESTION", "OWNER_IDENTITY_QUESTION"}:
        return "ANSWER_IDENTITY"
    if intent in {"STATUS_QUESTION", "LOCATION_QUESTION", "MESSAGE_READ_STATUS_QUESTION"}:
        return "DECLINE_TO_CONFIRM" if intent == "STATUS_QUESTION" else "PROTECT_PRIVACY"
    if intent in {"CONFIRMATION_QUESTION", "SCHEDULE_CONFIRMATION_QUESTION", "AMBIGUOUS_CONFIRMATION_QUESTION"}:
        return "DECLINE_TO_CONFIRM"
    if intent == "SCHEDULE_ACTION_REQUEST":
        return "DECLINE_TO_CONFIRM"
    if intent == "CALLBACK_REQUEST":
        return "ACKNOWLEDGE_REQUEST"
    if intent == "FUTURE_NOTIFICATION_REQUEST":
        return "OFFER_SUPPORTED_ALTERNATIVE"
    if audience == "recruiters" and intent in {"INTERVIEW_REQUEST", "SCHEDULING_PROPOSAL"}:
        if known_slots and all(key in known_slots.known for key in ("proposed_date", "proposed_time", "job_role")):
            return "ACKNOWLEDGE_REQUEST"
        return "COLLECT_INTERVIEW_DETAILS"
    if audience == "business_clients":
        return "COLLECT_SERVICE_DETAILS"
    if intent == "URGENCY":
        return "ACKNOWLEDGE_URGENCY"
    return "ASK_MISSING_SLOT"


def behavior_profile(spec: dict[str, Any] | None, *, allow_disabled: bool = False) -> dict[str, Any] | None:
    profile = (spec or {}).get("behavior")
    if not isinstance(profile, dict) or (profile.get("production_enabled") is False and not allow_disabled):
        return None
    return profile


def message_family(decision_type: str, recommended_action: str) -> str:
    if decision_type == "DO_NOT_RESPOND":
        return "DO_NOT_RESPOND"
    if decision_type in {"NO_POLICY", "INSUFFICIENT_CONTEXT"}:
        return "REQUEST_CONTEXT" if recommended_action == "request_information" else "FALLBACK"
    if decision_type == "ESCALATE":
        return "ESCALATION_NOTICE"
    if recommended_action in {"acknowledge", "respond"}:
        return "ACKNOWLEDGE"
    return "FALLBACK"


def _audience_style(profile: dict[str, Any], audience: str | None) -> dict[str, Any]:
    styles = profile.get("audience_styles") or {}
    return styles.get(audience) or styles.get("default") or {}


def _variant_id(family: str, parts: tuple[str, ...]) -> str:
    digest = sha256("|".join((family, *parts)).encode("utf-8")).hexdigest()[:12]
    return f"{family.lower()}-{digest}"


def _spoken(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def _text_has(text: str, *terms: str) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def _missing_slot(
    result: Any,
    inbound_text: str,
    audience: str | None,
    missing_information: list[str] | None,
    known_slots: ConversationSlots | None,
) -> str:
    explicit = [str(item).casefold() for item in (missing_information or getattr(result, "missing_information", None) or [])]
    known = known_slots.known if known_slots else {}
    if audience == "recruiters" and "proposed_date" in known and "proposed_time" in known and "job_role" not in known:
        return "job_role"
    if audience == "recruiters" and all(key in known for key in ("proposed_date", "proposed_time", "job_role")):
        return "__complete__"
    for slot in ("identity", "subject", "company", "interview_schedule", "urgency", "relationship_context", "privacy"):
        if any(slot in item for item in explicit):
            return slot
    if audience == "recruiters":
        if "company" in known and "job_role" not in known:
            return "job_role"
        if _text_has(inbound_text, "entrevista", "vaga", "oportunidade"):
            return "interview_schedule"
        return "company"
    if audience == "business_clients":
        if "impact" in known:
            return "urgency"
        return "service_problem" if _text_has(inbound_text, "problema", "serviço", "servico", "contrato") else "subject"
    if audience == "relationship" and _text_has(inbound_text, "está bem", "esta bem", "como ele"):
        return "relationship_context"
    if _text_has(
        inbound_text,
        "quem é", "quem e", "com quem estou falando", "quem fala",
        "qual é o seu nome", "qual e o seu nome", "como você se chama",
        "como voce se chama", "como se chama",
    ):
        return "identity"
    if _text_has(inbound_text, "em casa", "onde ele", "localização", "localizacao", "viu minha mensagem"):
        return "privacy"
    if audience in {"unknown", "friends", "family_core"} and not inbound_text.strip():
        return "identity"
    return "subject"


def _schedule_proposal(known: dict[str, str]) -> str:
    date = "amanhã" if known.get("proposed_date") == "tomorrow" else "o horário sugerido"
    time = known.get("proposed_time", "").replace(":00", "h")
    return f"Entendi a sugestão de {date}{f' às {time}' if time else ''}."


def _schedule_confirmation(known: dict[str, str]) -> str:
    if "proposed_date" in known or "proposed_time" in known:
        return "Essa sugestão de horário ainda não está confirmada."
    return "Eu ainda não tenho confirmação da agenda do Alex e não consigo marcar por conta própria."


def promise_guard(text: str, *, escalation_recorded: bool = False, registration_recorded: bool = False) -> str:
    checks = (
        (r"\b(vou|posso)\s+(avisar|pedir|sinalizar)\b", escalation_recorded),
        (r"\bele\s+vai\s+retornar\b", False),
        (r"\bassim que houver retorno\b", escalation_recorded),
        (r"\b(futura revisão|futura revisao)\b", False),
        (r"\bdeixar .* registrad[ao]\b", registration_recorded),
    )
    for pattern, supported in checks:
        if re.search(pattern, text, flags=re.IGNORECASE) and not supported:
            return "UNSUPPORTED_PROMISE"
    return "PROMISES_SUPPORTED"


def internal_language_leaks(text: str) -> list[str]:
    terms = ("policy", "decision", "intent", "pipeline", "autonomy", "context insufficient", "futura revisão", "futura revisao")
    lowered = text.casefold()
    return [term for term in terms if term in lowered]


def render_response(
    result: Any,
    profile: dict[str, Any],
    *,
    audience: str | None,
    introduced: bool = False,
    recent_variant_ids: set[str] | None = None,
    urgent: bool = False,
    inbound_text: str = "",
    missing_information: list[str] | None = None,
    known_slots: ConversationSlots | None = None,
    capabilities: dict[str, bool] | None = None,
    memory_context: dict[str, Any] | None = None,
) -> ResponseCandidate | None:
    family = message_family(str(result.decision_type), result.recommended_action)
    if family == "DO_NOT_RESPOND":
        return None
    style = _audience_style(profile, audience)
    bank = profile.get("message_variants") or {}
    entries = bank.get(family) or bank.get("FALLBACK") or {}
    greetings = style.get("greetings") or entries.get("greetings") or ["Oi!"]
    preferred_name = (memory_context or {}).get("preferred_name")
    if preferred_name and _text_has(inbound_text, "bom dia", "boa tarde", "boa noite", "oi", "olá", "ola"):
        greetings = [f"{greeting.rstrip('!.?')}, {preferred_name}!" for greeting in greetings]
    identities = style.get("identities") or entries.get("identities") or [profile.get("identity_statement", "Sou a assistente.")]
    availability = style.get("availability") or entries.get("availability") or ["Ele não consegue responder agora."]
    requests = style.get("requests") or entries.get("requests") or ["Qual é o assunto?"]
    closings = style.get("closings") or entries.get("closings") or [""]
    slot = _missing_slot(result, inbound_text, audience, missing_information, known_slots)
    intent = explicit_intent(inbound_text, known_slots)
    if intent == "IDENTITY_QUESTION":
        family = "IDENTITY"
    slot_questions = style.get("semantic_questions") or profile.get("semantic_questions") or {}
    requests = slot_questions.get(slot) or requests
    if slot == "__complete__":
        known = known_slots.known if known_slots else {}
        if known_slots and known_slots.schedule_status == "PROPOSED":
            requests = [
                f"{_schedule_proposal(known)} A vaga é de {known.get('job_role', 'seu cargo')}. Eu não consigo confirmar a agenda do Alex por conta própria."
            ]
        else:
            requests = [f"A vaga é de {known.get('job_role', 'seu cargo')}."]
    elif audience == "recruiters" and intent == "SCHEDULING_PROPOSAL" and known_slots:
        known = known_slots.known
        proposal = _schedule_proposal(known)
        if "job_role" not in known:
            requests = [f"{proposal} Eu não consigo confirmar a agenda do Alex por conta própria. Qual é a vaga?"]
        else:
            requests = [f"{proposal} A vaga é de {known['job_role']}. Eu não consigo confirmar a agenda do Alex por conta própria."]
    if urgent:
        requests = style.get("urgent_questions") or entries.get("urgent_requests") or requests
    escalation_recorded = bool(getattr(result, "escalation_recorded", False))
    if family == "ESCALATION_NOTICE" and not escalation_recorded:
        requests = entries.get("safe_requests") or ["Posso organizar o assunto para uma futura revisão."]
    include_intro = not introduced and profile.get("introduction_policy", "FIRST_CONTACT") != "NEVER"
    direct = (
        style.get("direct_responses", {}).get(slot)
        or entries.get("direct_responses", {}).get(slot)
        or profile.get("direct_responses", {}).get(slot)
        or (profile.get("direct_responses", {}).get(intent) if intent else None)
    )
    if intent == "SCHEDULE_ACTION_REQUEST":
        include_intro = not introduced
        requests = ["Eu não consigo marcar ou confirmar a agenda do Alex por conta própria."]
        direct = requests
    elif intent == "AMBIGUOUS_CONFIRMATION_QUESTION":
        include_intro = not introduced
        requests = ["Você quer confirmar qual informação?"]
        direct = requests
    elif intent == "MESSAGE_READ_STATUS_QUESTION":
        include_intro = False
        requests = ["Eu não consigo confirmar se o Alex já viu sua mensagem."]
        direct = requests
    elif intent == "SCHEDULE_CONFIRMATION_QUESTION":
        known = known_slots.known if known_slots else {}
        include_intro = not introduced
        requests = [_schedule_confirmation(known)]
        direct = requests
    elif intent in {"ASSISTANT_NATURE_QUESTION", "OWNER_IDENTITY_QUESTION"}:
        include_intro = False
        direct = (
            profile.get("direct_responses", {}).get(intent)
            or "Sou uma assistente virtual. Meu nome é Andy e ajudo o Alex com as mensagens quando ele não consegue responder."
        )
        requests = [direct] if isinstance(direct, str) else direct
    if direct and inbound_text.strip() and (slot in {"identity", "privacy"} or intent in {"IDENTITY_QUESTION", "STATUS_QUESTION", "LOCATION_QUESTION", "MESSAGE_READ_STATUS_QUESTION", "CONFIRMATION_QUESTION", "SCHEDULE_CONFIRMATION_QUESTION", "AMBIGUOUS_CONFIRMATION_QUESTION", "SCHEDULE_ACTION_REQUEST", "CALLBACK_REQUEST", "FUTURE_NOTIFICATION_REQUEST"}):
        include_intro = False if intent in {"IDENTITY_QUESTION", "ASSISTANT_NATURE_QUESTION", "OWNER_IDENTITY_QUESTION", "LOCATION_QUESTION", "MESSAGE_READ_STATUS_QUESTION", "STATUS_QUESTION"} else include_intro
        requests = direct
    if direct and audience == "relationship" and slot == "relationship_context":
        include_intro = False
        requests = direct
    candidates = []
    for greeting, identity, notice, request, closing in product(greetings, identities, availability, requests, closings):
        parts = (greeting, identity, notice, request, closing)
        text = " ".join(part.strip() for part in parts if part.strip())
        if not include_intro:
            text = " ".join(part.strip() for part in (request, closing) if part.strip())
        lowered_text = text.casefold()
        if any(lowered_text.count(phrase) > 1 for phrase in ("ajudando o alex", "mensagens do alex")):
            continue
        promise_check = promise_guard(text, escalation_recorded=escalation_recorded)
        if promise_check == "UNSUPPORTED_PROMISE" or internal_language_leaks(text):
            continue
        promise_check = "SUPPORTED_ESCALATION" if escalation_recorded else "NO_UNSUPPORTED_PROMISE"
        candidates.append(ResponseCandidate(family, _variant_id(family, (str(audience), *parts)), text, _spoken(text), include_intro, promise_check))
    recent = recent_variant_ids or set()
    start = (len(recent) * 37) % len(candidates) if candidates else 0
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        if candidate.variant_id not in recent:
            return candidate
    return candidates[0] if candidates else ResponseCandidate(
        family,
        _variant_id(family, (str(audience), "safe-fallback")),
        "Ainda não tenho informação suficiente para responder com segurança.",
        "Ainda não tenho informação suficiente para responder com segurança.",
        False,
        "NO_UNSUPPORTED_PROMISE",
    )


def variant_capacity(profile: dict[str, Any], family: str) -> int:
    entries = (profile.get("message_variants") or {}).get(family) or {}
    counts = [len(entries.get(key) or []) for key in ("greetings", "identities", "availability", "requests", "closings")]
    capacity = 1
    for count in counts:
        capacity *= max(count, 1)
    return capacity


def structural_variant_count(profile: dict[str, Any], family: str) -> int:
    entries = (profile.get("message_variants") or {}).get(family) or {}
    structures = entries.get("structures") or []
    audience_structures = sum(len((style.get("structures") or [])) for style in (profile.get("audience_styles") or {}).values())
    return len(structures) + audience_structures
