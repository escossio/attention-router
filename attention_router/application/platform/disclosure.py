"""Canonical read-only disclosure decision used by runtime and readiness."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class DisclosureAuthority:
    allowed: bool
    reason_code: str
    evaluated_at: datetime


def project_private_state_for_agent(authority: DisclosureAuthority, states: Iterable[dict]) -> list[dict]:
    """Allow only the authorized availability hint, never arbitrary private fields."""
    if not authority.allowed:
        return []
    return [
        {"namespace": "presence", "key": "effective",
         "value": {"status": state["value"].get("status"),
                   "audience_scope": state["value"]["audience_scope"]}}
        for state in states
        if state.get("namespace") == "presence" and state.get("key") == "effective"
        and isinstance(state.get("value"), dict)
        and state["value"].get("audience_scope") in {"all", "everyone"}
        and state["value"].get("status") in {"available", "busy", "sleeping", "away", "do_not_disturb", "custom"}
    ]


def evaluate_disclosure_authority(
    *,
    directives: Iterable[Any],
    allowed_disclosures: Iterable[str],
    operational_state: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> DisclosureAuthority:
    """Evaluate the existing presence-disclosure contract without mutation."""
    evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
    if not any(getattr(item, "effect_type", None) == "DISCLOSE_CURRENT_PRESENCE" for item in directives):
        return DisclosureAuthority(False, "DISCLOSURE_DIRECTIVE_MISSING", evaluated_at)
    if "availability_hint" not in set(allowed_disclosures):
        return DisclosureAuthority(False, "DISCLOSURE_POLICY_DENIED", evaluated_at)
    if not any(
        item.get("namespace") == "presence"
        and isinstance(item.get("value"), dict)
        and item["value"].get("audience_scope") in {"all", "everyone"}
        for item in operational_state
    ):
        return DisclosureAuthority(False, "DISCLOSURE_PRESENCE_UNAVAILABLE", evaluated_at)
    return DisclosureAuthority(True, "DISCLOSURE_ALLOWED", evaluated_at)
