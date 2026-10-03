"""Deterministic assistant identity/provenance for automated replies."""

from __future__ import annotations

import re


PROVENANCE_PREFIX = "[Andy]"


def label_automated_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return normalized
    if normalized.casefold().startswith(PROVENANCE_PREFIX.casefold()):
        return normalized
    return f"{PROVENANCE_PREFIX} {normalized}"


def ensure_introduction(
    text: str,
    *,
    introduced: bool,
    represented_reference_name: str | None,
) -> tuple[str, bool]:
    normalized = text.strip()
    if introduced or not normalized:
        return normalized, False
    if re.search(r"\b(?:eu\s+sou|sou)\s+a\s+andy\b", normalized, flags=re.IGNORECASE):
        return normalized, True
    if represented_reference_name:
        intro = f"Eu sou a Andy, assistente virtual de {represented_reference_name}."
    else:
        intro = "Eu sou a Andy, assistente virtual do titular da conta."
    return f"{intro} {normalized}", True


__all__ = ["PROVENANCE_PREFIX", "ensure_introduction", "label_automated_text"]
