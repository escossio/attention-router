from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SanitizationMode(StrEnum):
    REJECT = "REJECT"
    REDACT = "REDACT"


class PrivacyViolation(ValueError):
    def __init__(self, reason_code: str, *, path: str) -> None:
        self.reason_code = reason_code
        self.path = path
        super().__init__(f"{reason_code}:{path}")


@dataclass(frozen=True, slots=True)
class SanitizedValue:
    value: Any
    redacted_paths: tuple[str, ...]


_FORBIDDEN_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "browser_profile",
    "chain_of_thought",
    "cookie",
    "credential",
    "hmac",
    "local_auth",
    "localauth",
    "message_body",
    "password",
    "phone",
    "private_reasoning",
    "raw_message",
    "raw_payload",
    "secret",
    "session_material",
    "token",
    "whatsapp_body",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)^bearer\s+\S+$"),
    re.compile(r"(?i)^basic\s+\S+$"),
    re.compile(r"^sk-[A-Za-z0-9_-]{12,}$"),
)
_RAW_PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s().-]{7,}[0-9]$")
_REDACTED = "[REDACTED]"


def _canonical_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _forbidden_key(key: object) -> bool:
    canonical = _canonical_key(key)
    return any(fragment in canonical for fragment in _FORBIDDEN_KEY_FRAGMENTS)


def _forbidden_string(value: str) -> str | None:
    if _RAW_PHONE_PATTERN.fullmatch(value.strip()):
        return "RAW_PHONE_FORBIDDEN"
    if any(pattern.search(value.strip()) for pattern in _SECRET_VALUE_PATTERNS):
        return "SECRET_VALUE_FORBIDDEN"
    return None


def sanitize_metadata(
    value: Mapping[str, Any],
    *,
    mode: SanitizationMode = SanitizationMode.REJECT,
) -> SanitizedValue:
    """Validate platform metadata without copying sensitive operational payloads."""

    redacted: list[str] = []

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, Mapping):
            sanitized: dict[str, Any] = {}
            for key, child in item.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if _forbidden_key(key):
                    if mode is SanitizationMode.REJECT:
                        raise PrivacyViolation("FORBIDDEN_EVIDENCE_FIELD", path=child_path)
                    sanitized[key_text] = _REDACTED
                    redacted.append(child_path)
                    continue
                sanitized[key_text] = visit(child, child_path)
            return sanitized
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [visit(child, f"{path}[{index}]") for index, child in enumerate(item)]
        if isinstance(item, str):
            reason = _forbidden_string(item)
            if reason:
                if mode is SanitizationMode.REJECT:
                    raise PrivacyViolation(reason, path=path)
                redacted.append(path)
                return _REDACTED
        if isinstance(item, bytes | bytearray):
            if mode is SanitizationMode.REJECT:
                raise PrivacyViolation("BINARY_EVIDENCE_FORBIDDEN", path=path)
            redacted.append(path)
            return _REDACTED
        return item

    return SanitizedValue(value=visit(value, ""), redacted_paths=tuple(redacted))


def sanitized_summary(value: str | None, *, maximum_length: int = 320) -> str | None:
    """Accept a bounded technical summary, never a raw message or secret marker."""

    if value is None:
        return None
    normalized = " ".join(value.split())
    reason = _forbidden_string(normalized)
    if reason:
        raise PrivacyViolation(reason, path="summary")
    if len(normalized) > maximum_length:
        raise PrivacyViolation("SUMMARY_TOO_LONG", path="summary")
    return normalized
