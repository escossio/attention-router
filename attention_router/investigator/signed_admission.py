"""Offline HMAC admission proof for synthetic operational events only.

This is not a Zabbix webhook, an HTTP endpoint, or production authentication.
The tenant/source are selected by a trusted local credential registry; they
must never come from a user-supplied event body. In-memory replay protection
is intentionally NOT sufficient for deployments or multiple processes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import math
import re
from threading import Lock
from time import time
from typing import Callable, Mapping

from .model import SourceEvent

_ID = re.compile(r"^synthetic-[a-z0-9][a-z0-9-]{0,63}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SIG = re.compile(r"^[0-9a-f]{64}$")
_MEDIA = frozenset({"application/json", "application/octet-stream"})
_MAX_EVENT_BYTES = 65536
_MAX_CLOCK_SKEW = 120


class AdmissionRejected(ValueError):
    """Stable error only; no secrets, payloads or principal IDs in messages."""


def _deny() -> None:
    raise AdmissionRejected("ADMISSION_REJECTED")


@dataclass(frozen=True)
class SyntheticBinding:
    principal_id: str
    source: str
    tenant_id: str
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or _ID.fullmatch(value) is None
            for value in (self.principal_id, self.source, self.tenant_id)
        ):
            _deny()
        if type(self.key) is not bytes or len(self.key) < 32:
            _deny()


@dataclass(frozen=True)
class SignedEnvelope:
    """No caller-selected tenant or source field exists on this envelope."""

    principal_id: str
    issued_at: int
    nonce: str
    content_type: str
    source_event_id: str
    original: bytes = field(repr=False)
    signature_hex: str = field(repr=False)


def _length_prefixed(parts: tuple[bytes, ...]) -> bytes:
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _signed_bytes(binding: SyntheticBinding, envelope: SignedEnvelope) -> bytes:
    return _length_prefixed((
        b"INVESTIGATOR_SYNTHETIC_ADMISSION_V0",
        binding.principal_id.encode("ascii"),
        binding.source.encode("ascii"),
        binding.tenant_id.encode("ascii"),
        str(envelope.issued_at).encode("ascii"),
        envelope.nonce.encode("ascii"),
        envelope.content_type.encode("ascii"),
        envelope.source_event_id.encode("ascii"),
        sha256(envelope.original).digest(),
    ))


class SyntheticAdmission:
    """One-process fixture authority, fail closed if no binding exists."""

    def __init__(
        self, bindings: Mapping[str, SyntheticBinding],
        *,
        now: Callable[[], int | float] = time,
    ) -> None:
        if not bindings or any(
            name != item.principal_id for name, item in bindings.items()
        ):
            raise ValueError("INVALID_ADMISSION_REGISTRY")
        self._bindings = dict(bindings)
        self._now = now
        self._seen: set[tuple[str, str]] = set()
        self._lock = Lock()

    def admit(self, envelope: SignedEnvelope) -> SourceEvent:
        # Validate size/shape before any cryptographic or stateful work.
        if (
            not isinstance(envelope.principal_id, str)
            or not isinstance(envelope.nonce, str)
            or _NONCE.fullmatch(envelope.nonce) is None
            or type(envelope.issued_at) is not int
            or type(envelope.original) is not bytes
            or len(envelope.original) > _MAX_EVENT_BYTES
            or type(envelope.content_type) is not str
            or envelope.content_type not in _MEDIA
            or not isinstance(envelope.source_event_id, str)
            or _ID.fullmatch(envelope.source_event_id) is None
            or not isinstance(envelope.signature_hex, str)
            or _SIG.fullmatch(envelope.signature_hex) is None
        ):
            _deny()

        binding = self._bindings.get(envelope.principal_id)
        if binding is None:
            _deny()
        now = self._now()
        if type(now) not in (int, float):
            _deny()
        if type(now) is float and not math.isfinite(now):
            _deny()
        if abs(now - envelope.issued_at) > _MAX_CLOCK_SKEW:
            _deny()

        expected = hmac.new(
            binding.key, _signed_bytes(binding, envelope), sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, envelope.signature_hex):
            _deny()
        token = (binding.principal_id, envelope.nonce)
        with self._lock:
            if token in self._seen:
                _deny()
            # This state is NOT durable. Replace with an atomic persistent
            # replay ledger plus TTL, key lifecycle and outage handling.
            self._seen.add(token)

        return SourceEvent(
            source=binding.source,
            original=envelope.original,
            content_type=envelope.content_type,
            tenant_id=binding.tenant_id,
            source_event_id=envelope.source_event_id,
        )
