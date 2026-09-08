"""Rollback-independent, privacy-safe readiness diagnostics."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from attention_router.observability.tracing import current_span


logger = logging.getLogger("attention_router.readiness")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _signal_payload(signal: Any) -> dict[str, Any]:
    adapter_by_requirement = {
        "SYNTHETIC_WHATSAPP_READY": "synthetic_transport",
        "SYNTHETIC_ACTOR_READY": "pre_run_context",
        "SYNTHETIC_ACTOR_NOT_OWNER": "pre_run_context",
        "OWNER_TARGET_RESOLVED": "pre_run_context",
        "OWNER_PRESENCE_SLEEPING": "presence",
        "OWNER_PRESENCE_FRESH": "presence",
        "STANDING_DIRECTIVE_ACTIVE": "standing_directive",
        "DISCLOSURE_AUTHORITY_ALLOWED": "disclosure_authority",
        "RESPOND_PROVIDER_READY": "capability_provider",
        "ANDY_CONTEXT_READY": "andy_readiness",
        "PRODUCTION_EXECUTION_LEASE_READY": "execution_contract",
        "EXACTLY_ONCE_READY": "execution_contract",
        "CORRELATION_READY": "execution_contract",
    }
    name = str(signal.dependency_id)
    return {
        "name": name,
        "state": str(signal.state),
        "reason_code": str(signal.reason_code),
        "source": getattr(signal, "source", None) or adapter_by_requirement.get(name, "readiness.evaluator"),
        "evaluated_at": _timestamp(getattr(signal, "observed_at", None)),
        "freshness_expires_at": _timestamp(getattr(signal, "freshness_expires_at", None)),
    }


def emit_readiness_diagnostic(
    *,
    attempt_id: str,
    correlation_id: str,
    scenario_id: str,
    execution_level: str | None,
    attempt_started_at: datetime | None = None,
    evaluation: Any = None,
    handoff_result: str | None = None,
) -> None:
    """Emit one best-effort diagnostic without affecting the product path."""
    try:
        signals = tuple(getattr(evaluation, "evidence", ()) or ())
        domain = getattr(evaluation, "domain", None)
        state = getattr(domain, "state", None)
        state_value = getattr(state, "value", state or "UNKNOWN")
        blockers = tuple(getattr(domain, "blocker_references", ()) or ())
        payload = {
            "event_name": "attention.readiness.evaluation",
            "readiness_attempt_id": attempt_id,
            "correlation_id": correlation_id,
            "scenario_id": scenario_id,
            "execution_level": execution_level,
            "attempt_started_at": _timestamp(attempt_started_at),
            "evaluated_at": _timestamp(getattr(domain, "evaluated_at", None)),
            "readiness_state": state_value,
            "handoff_result": handoff_result,
            "signal_count": len(signals),
            "blocking_signal_count": len(blockers),
            "signals": [_signal_payload(signal) for signal in signals],
        }
        # JSON serialization is the allowlist boundary: arbitrary domain
        # metadata is never included in this event.
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        logger.warning("%s", encoded)
        span = current_span()
        if span.is_recording():
            span.add_event("attention.readiness.evaluation", {
                "readiness_attempt_id": attempt_id,
                "correlation_id": correlation_id,
                "scenario_id": scenario_id,
                "readiness_state": state_value,
                "blocking_signal_count": len(blockers),
                "blockers": list(blockers),
            })
    except Exception:
        # Telemetry must never turn a fail-closed readiness result into a
        # successful preparation or otherwise alter application behavior.
        logger.debug("readiness diagnostic emission failed", exc_info=True)


def new_readiness_attempt_id() -> str:
    return str(uuid4())
