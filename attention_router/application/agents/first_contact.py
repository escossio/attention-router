"""Small public identity realization inside the existing Agent response path."""

import re

from attention_router.application.agents.service import AndyAgentError
from attention_router.application.group_contextual_policy import LABEL

PRESENTATION_VERSION = "first-contact-v1"
_IDENTITY_QUESTION = re.compile(
    r"\b(?:quem [eé] voc[eê]|com quem (?:eu )?estou falando|"
    r"qual [eé] (?:o )?seu nome|como voc[eê] se chama|"
    r"voc[eê] [eé] (?:uma? )?(?:ia|rob[oô]|assistente virtual)|"
    r"who are you|what is your name|qui[eé]n eres)\b",
    re.I,
)
_SELF_INTRODUCTION = re.compile(
    r"\b(?:(?:eu )?sou (?:a |uma? )?(?:andy|assistente (?:virtual|automatizad[ao]))|"
    r"(?:eu )?me chamo andy|meu nome [eé] andy|aqui [eé] a? ?andy|"
    r"sou (?:uma? )?(?:ia|intelig[eê]ncia artificial)|"
    r"i(?:'m| am) (?:andy|(?:a |your )?(?:virtual |ai )?assistant)|"
    r"my name is andy|soy andy|me llamo andy)\b",
    re.I,
)


def agent_presentation(mode: str) -> dict[str, object]:
    return {
        "mode": mode,
        "presentation_version": PRESENTATION_VERSION,
        "identity_label": LABEL,
        "public_role": "virtual_assistant",
        "introduction_owned_by_router": True,
        "owner_information_available": False,
    }


def render_public_identity_response(
    text: str, *, mode: str, current_message: str, locale: str,
) -> str:
    """A server-owned prefix is not a model permission or delivery receipt."""
    if locale.startswith("en"):
        introduction = "Hi! I'm Andy, a virtual assistant on this account."
        identity = "I'm Andy, a virtual assistant on this account."
        offer = "I can help with general conversation."
    elif locale.startswith("es"):
        introduction = "¡Hola! Soy Andy, una asistente virtual en esta cuenta."
        identity = "Soy Andy, una asistente virtual en esta cuenta."
        offer = "Puedo ayudar con conversaciones generales."
    else:
        introduction = "Olá! Sou a Andy, uma assistente virtual nesta conta."
        identity = "Sou a Andy, uma assistente virtual nesta conta."
        offer = "Posso ajudar em conversas gerais."
    body = text.strip()
    if _IDENTITY_QUESTION.search(current_message):
        # Transparency on request is independent of the inaugural lifecycle.
        body = offer if mode == "INTRODUCE" else f"{identity} {offer}"
    elif LABEL in body or _SELF_INTRODUCTION.search(body):
        # Never trust a model to decide to reintroduce itself or add a second label.
        raise AndyAgentError("FIRST_CONTACT_UNSOLICITED_INTRODUCTION")
    if mode == "INTRODUCE":
        body = f"{introduction}\n\n{body}"
    return f"{LABEL} {body}"
