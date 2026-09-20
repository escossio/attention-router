from __future__ import annotations

import re
from typing import Any


PASSIVE_OBSERVATION_CONFIDENCE = 0.40
_PASSIVE_PROFANITY_RE = re.compile(
    r"(?i)\b(?:porra|caralho|merda)\b"
)


def detect_passive_idiolect_observations(
    text: str,
) -> tuple[tuple[str, dict[str, Any], float, str], ...]:
    """Return bounded passive language observations.

    These observations are intentionally low-confidence and must remain
    non-promotable on a single occurrence.
    """

    if not text.strip():
        return ()

    observations: list[tuple[str, dict[str, Any], float, str]] = []

    if _PASSIVE_PROFANITY_RE.search(text):
        observations.append(
            (
                "communication.observed.profanity_tolerance",
                {
                    "signal": "PROFANITY_PRESENT",
                    "direction": "USER_TO_ANDY_LANGUAGE",
                    "evidence_class": "OBSERVED",
                    "reuse_policy": "INTERPRET_ONLY",
                    "generalization_scope": "MESSAGE",
                    "generalization_confidence": 0.0,
                },
                PASSIVE_OBSERVATION_CONFIDENCE,
                "PASSIVE_OBSERVATION",
            )
        )

    return tuple(observations)


__all__ = [
    "PASSIVE_OBSERVATION_CONFIDENCE",
    "detect_passive_idiolect_observations",
]
