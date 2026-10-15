"""Deterministic authority for one explicitly enrolled WhatsApp group."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

POLICY_NAME = "GROUP_CONTEXTUAL_PRESENCE"
LABEL = "[Andy • IA]"
MODES = frozenset({"OFF", "OBSERVE", "MENTION_ONLY", "CONTEXTUAL"})

_MENTION = re.compile(r"(?<!\w)@?andy(?!\w)", re.IGNORECASE)
_TECHNICAL = re.compile(
    r"\b(?:agente|agentes|ia|intelig[eê]ncia artificial|modelo|prompt|api|"
    r"c[oó]digo|software|arquitetura|automa[cç][aã]o|llm|chatgpt|skynet)\b",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"(?:ignore|desconsidere|esque[cç]a).{0,50}(?:regras|instru[cç][oõ]es|pol[ií]tica|sistema)"
    r"|(?:revele|mostre|envie).{0,50}(?:segredo|senha|token|chave|prompt do sistema)"
    r"|(?:execute|rode).{0,30}(?:comando|shell|script)",
    re.IGNORECASE,
)
_STOP = re.compile(
    r"\b(?:andy[, :]*\s*)?(?:pare|parar|desligue|desative|stop|n[aã]o participe|"
    r"n[aã]o responda|n[aã]o processe)\b",
    re.IGNORECASE,
)


def relevance_kind(text: str) -> str | None:
    """Conservative prefilter; the model may still choose silence."""
    if _INJECTION.search(text):
        return None
    if _MENTION.search(text):
        return "MENTION"
    if _TECHNICAL.search(text) and ("?" in text or "skynet" in text.casefold()):
        return "CONTEXTUAL"
    return None


def requests_stop(text: str) -> bool:
    return bool(_STOP.search(text))


@dataclass(frozen=True)
class GroupAuthorityInput:
    configured_group_id: str | None
    destination: str
    mode: str
    session_ready: bool
    notice_sent: bool
    from_me: bool
    source_event_id: str
    interaction_id: str | None
    kind: str
    proposed_text: str
    confidence: str
    now: datetime
    recent_sent_at: tuple[datetime, ...] = ()
    duplicate: bool = False
    stopped: bool = False


def authorize_group_response(value: GroupAuthorityInput) -> str:
    if not value.configured_group_id or not value.configured_group_id.endswith("@g.us"):
        return "GROUP_ALLOWLIST_MISSING"
    if value.destination != value.configured_group_id:
        return "GROUP_NOT_ALLOWLISTED"
    if value.mode not in MODES or value.mode == "OFF" or value.stopped:
        return "GROUP_MODE_OFF"
    if value.mode == "OBSERVE":
        return "GROUP_OBSERVE_ONLY"
    if not value.session_ready:
        return "GROUP_SESSION_UNAVAILABLE"
    if not value.notice_sent:
        return "GROUP_NOTICE_REQUIRED"
    if value.from_me or not value.source_event_id or not value.interaction_id:
        return "GROUP_SOURCE_INVALID"
    if value.kind not in {"MENTION", "CONTEXTUAL"}:
        return "GROUP_RELEVANCE_INSUFFICIENT"
    if value.kind == "CONTEXTUAL" and value.mode != "CONTEXTUAL":
        return "GROUP_MENTION_ONLY"
    if value.confidence not in {"high", "medium"}:
        return "GROUP_CONFIDENCE_LOW"
    clean = value.proposed_text.strip()
    if not clean or len(clean) > 600 or _INJECTION.search(clean):
        return "GROUP_PROPOSAL_INVALID"
    if value.duplicate:
        return "GROUP_DUPLICATE_RESPONSE"
    last_hour = tuple(stamp for stamp in value.recent_sent_at if value.now - timedelta(hours=1) <= stamp <= value.now)
    if len(last_hour) >= 3:
        return "GROUP_HOURLY_LIMIT"
    cooldown = timedelta(minutes=2 if value.kind == "MENTION" else 20)
    if any(value.now - cooldown <= stamp <= value.now for stamp in last_hour):
        return "GROUP_COOLDOWN"
    return "ALLOW"
