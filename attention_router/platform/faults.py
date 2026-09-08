from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FaultInjectionDenied(RuntimeError):
    pass


class InjectedPlatformFault(RuntimeError):
    def __init__(self, fault_id: str, injection_point: str):
        super().__init__(f"{fault_id}:{injection_point}")
        self.fault_id = fault_id
        self.injection_point = injection_point


class FaultId(StrEnum):
    WORKER_BEFORE_LEASE_COMMIT = "FI-PE-001"
    WORKER_AFTER_LEASE_BUDGET_COMMIT = "FI-PE-002"
    WORKER_AFTER_EXECUTION_INTENT = "FI-PE-003"
    WORKER_AFTER_OUTBOX_PERSIST = "FI-PE-004"
    TRANSPORT_REQUEST_ACK_LOST = "FI-PE-005"
    LEDGER_SENT_FINALIZATION_LOST = "FI-PE-006"
    SCENARIO_RUNNER_AFTER_STIMULUS = "FI-PE-007"
    SYNTHETIC_DRIVER_AFTER_STIMULUS = "FI-PE-008"
    POSTGRES_UNAVAILABLE_DURING_CLAIM = "FI-PE-009"
    POSTGRES_UNAVAILABLE_DURING_FINALIZATION = "FI-PE-010"


@dataclass(frozen=True, slots=True)
class FaultDefinition:
    fault_id: FaultId
    target: str
    injection_point: str
    expected_safe_behavior: str
    requires_external_effect: bool = False
    requires_synthetic_driver: bool = False


FAULT_CATALOG: dict[str, FaultDefinition] = {
    item.fault_id.value: item
    for item in (
        FaultDefinition(
            FaultId.WORKER_BEFORE_LEASE_COMMIT,
            "WORKER",
            "BEFORE_LEASE_BUDGET_COMMIT",
            "transaction rolls back; same logical key may retry once",
        ),
        FaultDefinition(
            FaultId.WORKER_AFTER_LEASE_BUDGET_COMMIT,
            "WORKER",
            "AFTER_LEASE_BUDGET_COMMIT",
            "resume the same logical execution without another reservation",
        ),
        FaultDefinition(
            FaultId.WORKER_AFTER_EXECUTION_INTENT,
            "WORKER",
            "AFTER_EXECUTION_INTENT",
            "reconcile the same intent and outbox identity",
        ),
        FaultDefinition(
            FaultId.WORKER_AFTER_OUTBOX_PERSIST,
            "WORKER",
            "AFTER_OUTBOX_PERSIST",
            "dispatch only the existing idempotent outbox item",
        ),
        FaultDefinition(
            FaultId.TRANSPORT_REQUEST_ACK_LOST,
            "TRANSPORT",
            "AFTER_REQUEST_BEFORE_ACK",
            "mark ambiguous and prohibit blind replay",
        ),
        FaultDefinition(
            FaultId.LEDGER_SENT_FINALIZATION_LOST,
            "TRANSPORT_LEDGER",
            "AFTER_SENT_BEFORE_LOCAL_FINALIZE",
            "finalize from ledger without another send",
        ),
        FaultDefinition(
            FaultId.SCENARIO_RUNNER_AFTER_STIMULUS,
            "SCENARIO_RUNNER",
            "AFTER_STIMULUS",
            "restore the durable step and never repeat stimulus",
        ),
        FaultDefinition(
            FaultId.SYNTHETIC_DRIVER_AFTER_STIMULUS,
            "SYNTHETIC_DRIVER",
            "AFTER_STIMULUS",
            "reconcile or abort; never send spontaneously",
            requires_external_effect=True,
            requires_synthetic_driver=True,
        ),
        FaultDefinition(
            FaultId.POSTGRES_UNAVAILABLE_DURING_CLAIM,
            "POSTGRESQL",
            "DURING_CLAIM",
            "deny the effect; no memory-only claim",
        ),
        FaultDefinition(
            FaultId.POSTGRES_UNAVAILABLE_DURING_FINALIZATION,
            "POSTGRESQL",
            "DURING_FINALIZATION",
            "reread durable state before resume; no replay",
        ),
    )
}


class FaultInjector:
    """One-shot deterministic hooks for isolated tests; disabled outside explicit harnesses."""

    def __init__(self, *, enabled: bool = False, safe_environment: bool = False):
        self.enabled = enabled
        self.safe_environment = safe_environment
        self._armed: dict[str, int] = {}

    def arm(self, fault_id: str, *, count: int = 1) -> None:
        if not self.enabled or not self.safe_environment:
            raise FaultInjectionDenied("FAULT_INJECTION_REQUIRES_EXPLICIT_SAFE_HARNESS")
        if fault_id not in FAULT_CATALOG:
            raise FaultInjectionDenied("UNKNOWN_FAULT_INJECTION")
        if count != 1:
            raise FaultInjectionDenied("FAULT_INJECTION_V1_IS_ONE_SHOT")
        self._armed[fault_id] = count

    def checkpoint(self, fault_id: str) -> None:
        remaining = self._armed.get(fault_id, 0)
        if remaining <= 0:
            return
        self._armed[fault_id] = remaining - 1
        definition = FAULT_CATALOG[fault_id]
        raise InjectedPlatformFault(fault_id, definition.injection_point)

    def is_armed(self, fault_id: str) -> bool:
        return self._armed.get(fault_id, 0) > 0


def validate_fault_catalog() -> None:
    expected = {f"FI-PE-{number:03d}" for number in range(1, 11)}
    if set(FAULT_CATALOG) != expected:
        raise FaultInjectionDenied("FAULT_CATALOG_CARDINALITY_OR_ID_DRIFT")
