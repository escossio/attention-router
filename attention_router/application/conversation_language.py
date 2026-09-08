import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.voice_transcription import effective_interaction_text
from attention_router.infrastructure.models import InteractionRow


DEFAULT_CONVERSATION_LOCALE = "pt-BR"
HISTORY_INTERACTION_LIMIT = 6

_EXPLICIT_EN = re.compile(
    r"\b(?:answer|respond|reply|continue|switch|responda|fale|continue|mude)\b.{0,48}"
    r"\b(?:in english|english|em ingl[eê]s|para ingl[eê]s)\b",
    re.IGNORECASE,
)
_EXPLICIT_PT = re.compile(
    r"\b(?:answer|respond|reply|continue|switch|responda|fale|continue|volte|mude)\b.{0,48}"
    r"\b(?:in portuguese|portuguese|em portugu[eê]s|para portugu[eê]s)\b",
    re.IGNORECASE,
)
_PT_MARKERS = {
    "agora", "apenas", "acabou", "com", "como", "dez", "diga", "em", "e", "fale",
    "mais", "menos", "me", "qual", "quanto", "responda", "uma", "voce", "você",
}
_EN_MARKERS = {
    "answer", "back", "english", "five", "in", "me", "minus", "now", "only", "plus",
    "reply", "respond", "tell", "ten", "what", "with", "you",
}


@dataclass(frozen=True)
class ResolvedConversationLocale:
    locale: str
    source: str


def _explicit_locale(text: str) -> str | None:
    if _EXPLICIT_EN.search(text):
        return "en"
    if _EXPLICIT_PT.search(text):
        return "pt-BR"
    return None


def detect_message_locale(text: str) -> str | None:
    normalized = text.casefold()
    if re.search(r"[áàâãéêíóôõúç]", normalized):
        return "pt-BR"
    words = set(re.findall(r"[a-z]+", normalized))
    pt_score = len(words & _PT_MARKERS)
    en_score = len(words & _EN_MARKERS)
    if pt_score > en_score and pt_score >= 2:
        return "pt-BR"
    if en_score > pt_score and en_score >= 2:
        return "en"
    return None


def resolve_conversation_locale(
    current_message: str,
    recent_turns: list[dict[str, str]],
    fallback_locale: str = DEFAULT_CONVERSATION_LOCALE,
) -> ResolvedConversationLocale:
    explicit = _explicit_locale(current_message)
    if explicit:
        return ResolvedConversationLocale(explicit, "explicit_current_request")
    detected = detect_message_locale(current_message)
    if detected:
        return ResolvedConversationLocale(detected, "current_message")
    for turn in reversed(recent_turns):
        if turn.get("role") != "user":
            continue
        detected = detect_message_locale(turn.get("content", ""))
        if detected:
            return ResolvedConversationLocale(detected, "recent_user_turn")
    return ResolvedConversationLocale(fallback_locale, "configured_fallback")


def resolve_interaction_locale(
    session: Session,
    interaction: InteractionRow,
    fallback_locale: str = DEFAULT_CONVERSATION_LOCALE,
) -> ResolvedConversationLocale:
    previous = session.scalars(
        select(InteractionRow)
        .where(
            InteractionRow.tenant_id == interaction.tenant_id,
            InteractionRow.contact_id == interaction.contact_id,
            InteractionRow.created_at < interaction.created_at,
        )
        .order_by(InteractionRow.created_at.desc(), InteractionRow.id.desc())
        .limit(HISTORY_INTERACTION_LIMIT)
    ).all()
    recent_turns = [
        {"role": "user", "content": content}
        for row in reversed(previous)
        if (content := effective_interaction_text(session, row))
    ]
    return resolve_conversation_locale(
        effective_interaction_text(session, interaction),
        recent_turns,
        fallback_locale,
    )


def resolve_interaction_locale_by_id(
    session: Session,
    interaction_id: str,
    fallback_locale: str = DEFAULT_CONVERSATION_LOCALE,
) -> ResolvedConversationLocale:
    interaction = session.get(InteractionRow, interaction_id)
    if interaction is None:
        return ResolvedConversationLocale(fallback_locale, "configured_fallback")
    return resolve_interaction_locale(session, interaction, fallback_locale)
