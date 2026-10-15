"""Strict, synthetic-only observation projections for the Investigator SDK tool.

This V0 gate admits only explicitly modeled facts. A response with an
unrecognized field is rejected, not appended to a prompt or silently dropped.
The original Evidence remains in restricted investigation state for audit.
Real Zabbix, CI, Tempo and Linux contracts still require separate review.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
import re
from typing import Any

_ALLOWED_LEVELS = frozenset({"info", "warning", "high", "critical", "unknown"})
_ALLOWED_COVERAGE = frozenset({"none", "partial", "complete", "unknown"})
_ID = re.compile(r"^synthetic-[a-z0-9][a-z0-9-]{0,63}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_SERIES = 32
_MAX_JOBS = 16


class UnsafeToolObservation(ValueError):
    """Untrusted observation does not satisfy an explicit view contract."""


def _reject() -> None:
    # Never add the suspicious payload or individual field values to an exception.
    raise UnsafeToolObservation("TOOL_OBSERVATION_REJECTED")


def _mapping(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _reject()
    return value


def _string(value: Any, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        _reject()
    return value


def _synthetic_id(value: Any) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _reject()
    return value


def _instant(value: Any) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        _reject()
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            _reject()
    except ValueError:
        _reject()
    return value


def _number(value: Any, *, low: float = 0, high: float = 1e18) -> int | float:
    if type(value) not in (int, float):
        _reject()
    # Bound before float coercion so extreme integers do not overflow.
    if value < low or value > high or not isfinite(value):
        _reject()
    return value


def _list(value: Any, max_items: int) -> list[Any]:
    if type(value) is not list or len(value) > max_items:
        _reject()
    return value


def project_tool_observation(
    source: str, operation: str, observation: object,
) -> dict[str, object]:
    """Return an allowlisted, detached tool view or reject it entirely.

    This is deliberately NOT a generic secret regex or natural-language filter.
    Free-form tool messages, logs, trace attributes and unknown schemas are never
    passed to the model. They remain subject to separate restricted evidence rules.
    """
    if (source, operation) == ("zabbix", "read_event"):
        row = _mapping(
            observation, frozenset({"event_id", "severity", "observed_at"}),
        )
        return {
            "event_id": _synthetic_id(row["event_id"]),
            "severity": _string(row["severity"], _ALLOWED_LEVELS),
            "observed_at": _instant(row["observed_at"]),
        }

    if (source, operation) == ("zabbix", "read_history"):
        if type(observation) is not dict:
            _reject()
        if frozenset(observation) == frozenset({"cpu_pct"}):
            return {"cpu_pct": _number(observation["cpu_pct"], high=100)}
        row = _mapping(observation, frozenset({"series"}))
        series = _list(row["series"], _MAX_SERIES)
        return {"series": [
            {
                "time": _instant(
                    _mapping(item, frozenset({"time", "value"}))["time"],
                ),
                "value": _number(item["value"]),
            }
            for item in series
        ]}

    if (source, operation) == ("tempo", "read_trace"):
        row = _mapping(
            observation, frozenset({"matched_spans", "coverage", "observed_at"}),
        )
        # Detailed spans require their own audited contract; do not leak tags
        # or baggage by forwarding arbitrary span dictionaries.
        if _list(row["matched_spans"], 0):
            _reject()
        return {
            "matched_spans": [],
            "coverage": _string(row["coverage"], _ALLOWED_COVERAGE),
            "observed_at": _instant(row["observed_at"]),
        }

    if (source, operation) == ("distributed_ci", "read_jobs"):
        row = _mapping(observation, frozenset({"jobs", "data_class"}))
        _string(row["data_class"], frozenset({"metadata-only"}))
        jobs = []
        for item in _list(row["jobs"], _MAX_JOBS):
            job = _mapping(
                item, frozenset({"job_id", "started_at", "ended_at", "worker"}),
            )
            jobs.append({
                "job_id": _synthetic_id(job["job_id"]),
                "started_at": _instant(job["started_at"]),
                "ended_at": _instant(job["ended_at"]),
                "worker": _synthetic_id(job["worker"]),
            })
        return {"jobs": jobs, "data_class": "metadata-only"}

    # No generic fallback. Additional operations must be separately contracted.
    _reject()
