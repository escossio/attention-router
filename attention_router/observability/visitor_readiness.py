"""AR-1002: pure visitor-readiness evidence reducer, no I/O or side effects.

This module does not assert real WhatsApp delivery or execute the agent.
"""
from __future__ import annotations

from collections.abc import Mapping

REQUIRED = (
    "worker_healthy",
    "visitor_flag_enabled",
    "default_configured",
    "blueprint_exists",
    "tenant_matches",
    "version_belongs_to_blueprint",
    "blueprint_published",
    "version_immutable",
    "version_checksum_valid",
    "contract_text_only_v1",
    "policy_active",
    "policy_immutable",
    "policy_checksum_valid",
    "policy_text_only",
    "capabilities_empty",
    "disclosures_empty",
    "guard_installed",
)
# A proven false for any essential contract property is a definite violation.
# Missing evidence is not a healthy result; telemetry freshness is independent.
def classify(evidence: Mapping[str, bool | None], *, collector_ok: bool,
             snapshot_consistent: bool, maintenance: bool = False) -> dict:
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be a mapping")
    if any(key not in REQUIRED for key in evidence):
        raise ValueError("unexpected evidence key")
    if any(value is not None and type(value) is not bool for value in evidence.values()):
        raise ValueError("evidence must be true, false, or null")
    missing = sorted(set(REQUIRED) - set(evidence))
    false = sorted(k for k in REQUIRED if evidence.get(k) is False)
    if not collector_ok or not snapshot_consistent:
        state, reason = "UNKNOWN", "SOURCE_UNAVAILABLE_OR_INCONSISTENT"
    elif maintenance:
        state, reason = "UNKNOWN", "MAINTENANCE_WINDOW"
    elif false:
        state, reason = "BROKEN", "CONTRACT_VIOLATION"
    elif missing or any(evidence[k] is None for k in REQUIRED):
        state, reason = "DEGRADED", "INCOMPLETE_EVIDENCE"
    else:
        state, reason = "READY", "CONTRACT_VERIFIED"
    return {
        "code": "AR-1002",
        "state": state,
        "reason_code": reason,
        "failed_checks": false if state == "BROKEN" else [],
        "missing_checks": missing if state == "DEGRADED" else [],
        "configuration_only": True,
        "whatsapp_delivery_proven": False,
        "model_execution_proven": False,
        "source": "SYNTHETIC_INPUT_ONLY",
    }
