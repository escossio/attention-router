from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ATTENTION_LOGIC_POLICY_ID = "attention_logic_v1"
ATTENTION_LOGIC_POLICY_VERSION = "attention_logic_v1:1"

URGENCY_KEYWORDS = {
    "agora",
    "urgente",
    "urgencia",
    "urgência",
    "emergencia",
    "emergência",
    "socorro",
}


@dataclass(frozen=True)
class AttentionThresholds:
    recent_window_seconds: int
    rapid_repeat_seconds: int
    persistent_message_count: int
    short_text_max_chars: int
    medium_text_max_chars: int


@dataclass(frozen=True)
class ActorProfile:
    actor_alias: str
    display_name: str
    relationship: str
    role: str | None
    priority: str
    test_allowed: bool
    policy_id: str


@dataclass(frozen=True)
class InteractionSummary:
    interaction_id: str
    created_at: datetime


def text_length_bucket(text: str, short_max: int, medium_max: int) -> str:
    size = len(text.strip())
    if size <= short_max:
        return "short"
    if size <= medium_max:
        return "medium"
    return "long"


def has_urgency_keyword(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in URGENCY_KEYWORDS)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def classify_inbound(
    text: str,
    actor_priority: str,
    current_interaction: InteractionSummary,
    recent_interactions: list[InteractionSummary],
    thresholds: AttentionThresholds,
) -> dict[str, Any]:
    previous = [item for item in recent_interactions if item.interaction_id != current_interaction.interaction_id]
    previous.sort(key=lambda item: item.created_at, reverse=True)
    seconds_since_previous = None
    if previous:
        seconds_since_previous = int(
            (_aware_utc(current_interaction.created_at) - _aware_utc(previous[0].created_at)).total_seconds()
        )
        if seconds_since_previous < 0:
            seconds_since_previous = 0
    message_count_recent = len(previous) + 1
    is_repeated_contact = message_count_recent >= 2
    is_rapid_repeat = seconds_since_previous is not None and seconds_since_previous <= thresholds.rapid_repeat_seconds
    classification = "normal"
    if message_count_recent >= thresholds.persistent_message_count:
        classification = "persistent"
    elif is_repeated_contact:
        classification = "repeated"
    if is_rapid_repeat:
        classification = "rapid_repeat" if classification != "persistent" else "persistent_rapid_repeat"
    return {
        "message_count_recent": message_count_recent,
        "seconds_since_previous_message": seconds_since_previous,
        "is_repeated_contact": is_repeated_contact,
        "is_rapid_repeat": is_rapid_repeat,
        "text_length_bucket": text_length_bucket(
            text, thresholds.short_text_max_chars, thresholds.medium_text_max_chars
        ),
        "has_question_mark": "?" in text,
        "has_urgency_keyword": has_urgency_keyword(text),
        "actor_priority": actor_priority,
        "classification": classification,
    }


def decide_attention(signals: dict[str, Any]) -> dict[str, Any]:
    reason_codes: list[str] = []
    high_priority = signals["actor_priority"] == "high"
    repeated = signals["is_repeated_contact"]
    rapid = signals["is_rapid_repeat"]
    persistent = signals["message_count_recent"] >= 3
    urgent = signals["has_urgency_keyword"]

    if high_priority:
        reason_codes.append("KNOWN_HIGH_PRIORITY_ACTOR")
    if rapid:
        reason_codes.append("RAPID_REPEAT")
    if persistent:
        reason_codes.append("MULTIPLE_MESSAGES_RECENTLY")
    if urgent:
        reason_codes.append("URGENCY_KEYWORD")
    if not reason_codes and not repeated:
        reason_codes.append("NORMAL_SINGLE_MESSAGE")

    attention_level = "normal"
    if high_priority and not repeated:
        attention_level = "medium"
    if (high_priority and rapid) or (urgent and repeated) or persistent:
        attention_level = "high"

    suggested_action = "none"
    if attention_level == "medium":
        suggested_action = "review_when_available"
    elif attention_level == "high":
        suggested_action = "notify_operator"

    return {
        "attention_level": attention_level,
        "reason_codes": reason_codes,
        "suggested_action": suggested_action,
        "suggested_reply_profile": "none",
        "escalation_candidate": attention_level == "high",
        "auto_action_enabled": False,
    }
