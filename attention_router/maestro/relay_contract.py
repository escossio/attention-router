"""Offline MAESTRO relay-envelope validation.

This is a reusable contract module, NOT an HTTP endpoint, admission grant,
database migration, background worker or MAESTRO implementation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_RELAY_BODY_BYTES = 16384
RELAY_CONTRACT_VERSION = "occurrence-relay-v0-draft"

_ENVELOPE_KEYS = frozenset({
    "contract_version", "occurrence_number", "source_system", "tenant_id", "signal",
})
_SIGNAL_REQUIRED = frozenset({
    "schema_version", "event_id", "occurred_at", "asset_ref", "signal_code", "status",
})
_SIGNAL_ALLOWED = _SIGNAL_REQUIRED | {
    "problem_event_id", "service_ref", "interface_ref", "severity", "summary",
}
_ALLOWED_STATUS = frozenset({"PROBLEM", "RECOVERY", "UPDATE"})


class RelayContractRejected(ValueError):
    """Schema / authority mismatch. Never include untrusted values in messages."""


@dataclass(frozen=True, slots=True)
class TrustedRelayBinding:
    """MUST be created by authenticated service-binding code, never from JSON."""

    tenant_id: str
    source_system: str
    instance_id: str

    def __post_init__(self):
        for value in (self.tenant_id, self.source_system, self.instance_id):
            if type(value) is not str or not 1 <= len(value) <= 120 or not value.strip():
                raise ValueError("invalid trusted binding")


@dataclass(frozen=True, slots=True)
class ValidatedRelayEvent:
    tenant_id: str
    source_system: str
    instance_id: str
    occurrence_number: int
    idempotency_key: str
    source_event_id: str
    status: str
    occurred_at: datetime
    digest: str
    signal: dict[str, Any]

    @property
    def receipt_identity(self) -> tuple[str, str, str, int]:
        """Scoped key for a FUTURE atomic, durable MAESTRO receipt store."""
        return (self.tenant_id, self.source_system, self.instance_id, self.occurrence_number)


def _unique_pairs(items):
    found = {}
    for key, value in items:
        if key in found:
            raise RelayContractRejected("duplicate JSON key")
        found[key] = value
    return found


def _strict_string(value: Any, limit: int):
    if type(value) is not str or not 1 <= len(value) <= limit or not value.strip():
        raise RelayContractRejected("invalid typed field")


def _timestamp(value: Any) -> datetime:
    _strict_string(value, 45)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelayContractRejected("invalid occurrence time") from exc
    if parsed.utcoffset() is None:
        raise RelayContractRejected("timestamp requires timezone")
    return parsed


def _check_json_depth(value: Any):
    pending = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if level > 12:
            raise RelayContractRejected("JSON nesting limit exceeded")
        if type(item) is float and not math.isfinite(item):
            raise RelayContractRejected("invalid number")
        if type(item) is dict:
            pending.extend((part, level + 1) for part in item.values())
        elif type(item) is list:
            pending.extend((part, level + 1) for part in item)


def validate_relay_envelope(
    body: bytes,
    *,
    authenticated_binding: TrustedRelayBinding,
    idempotency_header: str,
) -> ValidatedRelayEvent:
    """Validate an already-authenticated relay payload without any side effects.

    The future MAESTRO HTTP boundary must verify the bearer *before* calling
    this function, enforce limits/duplicate headers and derive the trusted
    binding from its service identity. A valid value alone is NOT an ACK.
    """
    if type(body) is not bytes or not 0 < len(body) <= MAX_RELAY_BODY_BYTES:
        raise RelayContractRejected("invalid body length")
    if type(idempotency_header) is not str:
        raise RelayContractRejected("invalid idempotency header")
    if not isinstance(authenticated_binding, TrustedRelayBinding):
        raise RelayContractRejected("trusted binding required")
    try:
        envelope = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _value: (_ for _ in ()).throw(
                                  RelayContractRejected("invalid number")))
        _check_json_depth(envelope)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RelayContractRejected("invalid relay JSON") from exc
    if type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS:
        raise RelayContractRejected("invalid envelope shape")
    if envelope["contract_version"] != RELAY_CONTRACT_VERSION:
        raise RelayContractRejected("unexpected contract version")
    number = envelope["occurrence_number"]
    if type(number) is not int or not 1 <= number <= 9223372036854775807:
        raise RelayContractRejected("invalid occurrence number")
    _strict_string(envelope["tenant_id"], 120)
    _strict_string(envelope["source_system"], 120)
    if (not hmac.compare_digest(envelope["tenant_id"], authenticated_binding.tenant_id)
            or not hmac.compare_digest(envelope["source_system"], authenticated_binding.source_system)):
        raise RelayContractRejected("source or tenant not authorized")
    expected_key = f"{authenticated_binding.source_system}:{number}"
    if not hmac.compare_digest(idempotency_header, expected_key):
        raise RelayContractRejected("idempotency key mismatch")
    signal = envelope["signal"]
    if type(signal) is not dict or not _SIGNAL_REQUIRED.issubset(signal) or set(signal) - _SIGNAL_ALLOWED:
        raise RelayContractRejected("invalid signal keys")
    if signal["schema_version"] != "1" or signal["status"] not in _ALLOWED_STATUS:
        raise RelayContractRejected("invalid signal version/status")
    for name in ("event_id", "asset_ref", "signal_code"):
        _strict_string(signal[name], 160)
    for name in ("problem_event_id", "service_ref", "interface_ref", "summary"):
        if name in signal:
            _strict_string(signal[name], 500)
    if signal["status"] == "RECOVERY" and "problem_event_id" not in signal:
        raise RelayContractRejected("recovery missing problem event")
    if "severity" in signal and (type(signal["severity"]) is not int or
                                 not 0 <= signal["severity"] <= 5):
        raise RelayContractRejected("invalid severity")
    occurred_at = _timestamp(signal["occurred_at"])
    canonical = json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return ValidatedRelayEvent(
        tenant_id=authenticated_binding.tenant_id,
        source_system=authenticated_binding.source_system,
        instance_id=authenticated_binding.instance_id,
        occurrence_number=number,
        idempotency_key=expected_key,
        source_event_id=signal["event_id"],
        status=signal["status"],
        occurred_at=occurred_at,
        digest=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        signal=signal.copy(),
    )
