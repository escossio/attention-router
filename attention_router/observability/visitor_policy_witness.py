"""AR-1002: verify a policy resolver witness without exposing identity.

No SQL, no network, no contact identifiers in output. Caller must provide a
read-only, consistent snapshot and the same resolver semantics as production.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any


def verify_unknown_policy(*, policies: Sequence[Any], resolve: Callable[..., Any],
                          witness: dict[str, Any], expected_policy_id: str | None,
                          snapshot_consistent: bool) -> bool | None:
    """None means unverifiable, False means a proven mismatch, True a match.

    The witness must be an explicitly authorized synthetic visitor fixture.
    This cannot establish coverage for all possible contact/context criteria.
    """
    if not snapshot_consistent or not expected_policy_id:
        return None
    if not isinstance(witness, dict) or set(witness) != {
        "contact_id", "category", "active_context", "audience", "binding_id"
    }:
        return None
    if (witness["audience"] != "unknown" or witness["binding_id"] is not None
            or not isinstance(witness["contact_id"], str)
            or not witness["contact_id"].startswith("synthetic-")
            or not isinstance(witness["category"], str)):
        return None
    try:
        resolution = resolve(policies, witness["contact_id"], witness["category"],
                             witness["active_context"], audience="unknown", binding_id=None)
        winner = getattr(getattr(resolution, "winner", None), "identifier", None)
    except (KeyError, ValueError, TypeError):
        return None
    return winner == expected_policy_id if isinstance(winner, str) else None
