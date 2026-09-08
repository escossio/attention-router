"""Narrow organic/direct text authority, independent of capability authority."""

from dataclasses import dataclass
import re
from typing import Any

DIRECT_TEXT_CONVERSATION_CONTRACT_VERSION = "v1"
DIRECT_TEXT_ACTIONS = frozenset({"respond", "request_information"})
_PEER = re.compile(r"[0-9]+@(c\.us|lid)\Z")


@dataclass(frozen=True)
class DirectConversationEligibility:
    eligible: bool
    reason_code: str
    conversation_key: str | None = None
    source_account: str | None = None
    peer_reference: str | None = None  # Internal routing only; never log or prompt.
    peer_kind: str | None = None
    return_channel_available: bool = False


def direct_conversation_eligibility(event) -> DirectConversationEligibility:
    if event is None:
        return DirectConversationEligibility(False, "DIRECT_EVENT_MISSING")
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    checks = (
        (event.source == "wwebjs", "DIRECT_SOURCE_REQUIRED"),
        (payload.get("channel") == "whatsapp", "DIRECT_CHANNEL_REQUIRED"),
        (
            getattr(event, "event_type", None) == "message"
            and payload.get("event_type") == "message",
            "DIRECT_MESSAGE_REQUIRED",
        ),
        (payload.get("event_origin") == "EXTERNAL_INBOUND", "DIRECT_EXTERNAL_REQUIRED"),
        (
            payload.get("owner_authenticated") is False
            and metadata.get("from_me") is False
            and not payload.get("from_me")
            and not payload.get("fromMe")
            and not metadata.get("fromMe")
            and not metadata.get("owner_self_chat"),
            "DIRECT_SELF_DENIED",
        ),
        (event.lineage_classification == "ORGANIC", "DIRECT_ORGANIC_REQUIRED"),
        (
            not any(
                payload.get(k) or getattr(event, k, None)
                for k in ("scenario_id", "scenario_run_id", "scenario_step_run_id", "stimulus_id")
            ),
            "DIRECT_ORGANIC_REQUIRED",
        ),
        (metadata.get("conversation_state") == "READY", "DIRECT_CONVERSATION_UNRESOLVED"),
        (not metadata.get("is_group"), "DIRECT_GROUP_DENIED"),
    )
    for passed, reason in checks:
        if not passed:
            return DirectConversationEligibility(False, reason)
    peer = payload.get("external_actor_id") or payload.get("actor_id")
    key = metadata.get("conversation_key")
    account = metadata.get("source_account")
    if not isinstance(peer, str) or not _PEER.fullmatch(peer):
        return DirectConversationEligibility(False, "DIRECT_PEER_INVALID")
    if not isinstance(key, str) or not key.startswith("wwebjs:") or not _PEER.fullmatch(key[7:]):
        return DirectConversationEligibility(False, "DIRECT_CONVERSATION_KEY_INVALID")
    aliases = metadata.get("peer_identifiers") or [peer]
    if not isinstance(aliases, list) or any(
        not isinstance(x, str) or not _PEER.fullmatch(x) for x in aliases
    ):
        return DirectConversationEligibility(False, "DIRECT_ALIASES_INVALID")
    if peer not in aliases or key[7:] not in aliases:
        return DirectConversationEligibility(False, "DIRECT_PEER_MISMATCH")
    if metadata.get("peer_id_kind") not in {None, key.split("@")[1]}:
        return DirectConversationEligibility(False, "DIRECT_PEER_KIND_MISMATCH")
    if not isinstance(account, str) or not account.strip():
        return DirectConversationEligibility(False, "DIRECT_SOURCE_ACCOUNT_REQUIRED")
    return DirectConversationEligibility(
        True, "DIRECT_ELIGIBLE", key, account, peer, peer.split("@")[1], True
    )


def conversational_policy_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Project text-only authority; never modify the selected policy or its grants.

    Explicit conversational_execution is authoritative (invalid values deny).
    Legacy modes with explicit text actions are also restrictive, including scope.
    Historical attention actions alone are not a conversational contract.
    """
    config = config or {}
    explicit = config.get("conversational_execution")
    if "conversational_execution" in config:
        return explicit if isinstance(explicit, dict) else {"execution_mode": "OBSERVE"}
    if config.get("execution_mode") is not None and DIRECT_TEXT_ACTIONS.intersection(
        config.get("allowed_actions") or []
    ):
        return config
    return {"execution_mode": "AUTO_ALLOWED", "allowed_actions": sorted(DIRECT_TEXT_ACTIONS)}
