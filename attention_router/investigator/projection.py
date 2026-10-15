"""Minimal, strictly typed incident symptoms for an agent prompt.

This is a *transport contract*, not proof that the source was authenticated.
A future admission authority must supply a projection derived from authorized
metadata. The investigator never parses event.original to construct it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re

from .model import SourceEvent

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_SYMPTOMS = frozenset({
    "resource_pressure", "service_unavailable", "latency_increase",
    "disk_pressure", "network_degradation", "unknown",
})
_SEVERITIES = frozenset({"info", "warning", "high", "critical", "unknown"})
_ROLES = frozenset({"ci_worker", "application", "database", "network", "unknown"})
_UNITS = frozenset({"percent", "milliseconds", "bytes", "count", "none"})


@dataclass(frozen=True, slots=True)
class IncidentProjection:
    """Enumerated fields only; no hostnames, tenant IDs or free-text log data."""

    event_sha256: str
    symptom: str
    severity: str
    observed_at: str
    target_role: str = "unknown"
    observed_value: float | None = None
    unit: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.event_sha256, str) or not _SHA256_RE.fullmatch(
            self.event_sha256
        ):
            raise ValueError("INVALID_PROJECTION_DIGEST")
        for name, allowed in (
            ("symptom", _SYMPTOMS), ("severity", _SEVERITIES),
            ("target_role", _ROLES), ("unit", _UNITS),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"INVALID_PROJECTION_{name.upper()}")
        if not isinstance(self.observed_at, str) or not _TIMESTAMP_RE.fullmatch(
            self.observed_at
        ):
            raise ValueError("INVALID_PROJECTION_TIMESTAMP")
        try:
            stamp = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("INVALID_PROJECTION_TIMESTAMP") from exc
        if stamp.utcoffset() is None:
            raise ValueError("INVALID_PROJECTION_TIMESTAMP")
        number = self.observed_value
        if number is not None:
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError("INVALID_PROJECTION_VALUE")
            if not math.isfinite(number):
                raise ValueError("INVALID_PROJECTION_VALUE")
            if self.unit == "none":
                raise ValueError("INVALID_PROJECTION_UNIT")
        elif self.unit != "none":
            raise ValueError("INVALID_PROJECTION_UNIT")

    def for_event(self, event: SourceEvent) -> dict[str, object]:
        """Fail closed on cross-event reuse; only bounded values reach the model."""
        if self.event_sha256 != event.digest:
            raise ValueError("PROJECTION_EVENT_MISMATCH")
        return {
            "mode": "synthetic_read_only_v0",
            "event_sha256": self.event_sha256,
            "symptom": self.symptom,
            "severity": self.severity,
            "observed_at": self.observed_at,
            "target_role": self.target_role,
            "observed_value": self.observed_value,
            "unit": self.unit,
        }
