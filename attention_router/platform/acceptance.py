from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class AcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class AcceptanceVehicle(StrEnum):
    NORMAL_TEST = "NORMAL_TEST"
    SCENARIO = "SCENARIO"
    FAULT_INJECTION = "FAULT_INJECTION"
    POST_APPLY = "POST_APPLY"
    HUMAN_GOVERNANCE = "HUMAN_GOVERNANCE"


@dataclass(frozen=True, slots=True)
class AcceptanceDefinition:
    acceptance_id: str
    pe_ids: tuple[str, ...]
    package_id: str
    primary_vehicle: AcceptanceVehicle
    promotion_blocking: bool = True
    mandatory: bool = True


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    acceptance_id: str
    status: AcceptanceStatus
    reason_code: str
    evidence_references: tuple[str, ...]
    observed_summary: str | None = None


_SHARED: tuple[AcceptanceDefinition, ...] = (
    *(AcceptanceDefinition(f"ACC-CORE-LEASE-{n:03d}", ("PE-009", "PE-018"), "WP-PE-03", AcceptanceVehicle.NORMAL_TEST) for n in range(1, 6)),
    *(AcceptanceDefinition(f"ACC-CORE-BUDGET-{n:03d}", ("PE-009", "PE-018"), "WP-PE-03", AcceptanceVehicle.NORMAL_TEST) for n in range(1, 5)),
    AcceptanceDefinition("ACC-CORE-TX-001", ("PE-009", "PE-018"), "WP-PE-03", AcceptanceVehicle.FAULT_INJECTION),
    *(AcceptanceDefinition(f"ACC-CORE-SEND-{n:03d}", ("PE-009", "PE-017"), "WP-PE-03", AcceptanceVehicle.SCENARIO) for n in range(1, 4)),
    *(AcceptanceDefinition(f"ACC-CORE-READY-{n:03d}", ("PE-001", "PE-002", "PE-012"), "WP-PE-01", AcceptanceVehicle.NORMAL_TEST) for n in range(1, 4)),
    AcceptanceDefinition("ACC-CORE-AUTH-001", ("PE-007", "PE-009", "PE-017"), "WP-PE-04", AcceptanceVehicle.NORMAL_TEST),
    AcceptanceDefinition("ACC-CORE-TENANT-001", ("PE-001", "PE-003", "PE-005", "PE-007", "PE-009"), "WP-PE-01", AcceptanceVehicle.NORMAL_TEST),
    AcceptanceDefinition("ACC-CORE-PRIV-001", ("PE-001", "PE-006", "PE-007", "PE-012"), "WP-PE-01", AcceptanceVehicle.NORMAL_TEST),
    AcceptanceDefinition("ACC-CORE-PROV-001", ("PE-001", "PE-003", "PE-008", "PE-012", "PE-016", "PE-017"), "WP-PE-01", AcceptanceVehicle.POST_APPLY),
)

_SPECIFIC_COUNTS = {
    "PE-001": 5,
    "PE-002": 5,
    "PE-003": 4,
    "PE-004": 6,
    "PE-005": 5,
    "PE-006": 6,
    "PE-007": 7,
    "PE-008": 8,
    "PE-009": 10,
    "PE-010": 5,
    "PE-011": 6,
    "PE-012": 5,
    "PE-013": 4,
    "PE-014": 5,
    "PE-015": 5,
    "PE-016": 7,
    "PE-017": 8,
    "PE-018": 7,
}

_PACKAGE_BY_PE = {
    "PE-001": "WP-PE-01", "PE-002": "WP-PE-01", "PE-003": "WP-PE-01",
    "PE-004": "WP-PE-02", "PE-005": "WP-PE-02", "PE-006": "WP-PE-12",
    "PE-007": "WP-PE-04", "PE-008": "WP-PE-06", "PE-009": "WP-PE-05",
    "PE-010": "WP-PE-05", "PE-011": "WP-PE-05", "PE-012": "WP-PE-01",
    "PE-013": "WP-PE-07", "PE-014": "WP-PE-08", "PE-015": "WP-PE-09",
    "PE-016": "WP-PE-10", "PE-017": "WP-PE-11", "PE-018": "WP-PE-13",
}


def _vehicle_for(pe_id: str, ordinal: int) -> AcceptanceVehicle:
    if pe_id == "PE-008" and ordinal in {6, 7, 8}:
        return AcceptanceVehicle.SCENARIO
    if pe_id == "PE-009" and ordinal in {6, 9, 10}:
        return AcceptanceVehicle.SCENARIO
    if pe_id == "PE-016" and ordinal == 3:
        return AcceptanceVehicle.FAULT_INJECTION
    if pe_id == "PE-017" and ordinal in {2, 7}:
        return AcceptanceVehicle.HUMAN_GOVERNANCE
    if pe_id == "PE-017" and ordinal == 8:
        return AcceptanceVehicle.POST_APPLY
    return AcceptanceVehicle.NORMAL_TEST


_SPECIFIC = tuple(
    AcceptanceDefinition(
        acceptance_id=f"ACC-PE{pe_id[-3:]}-{ordinal:03d}",
        pe_ids=(pe_id,),
        package_id=_PACKAGE_BY_PE[pe_id],
        primary_vehicle=_vehicle_for(pe_id, ordinal),
    )
    for pe_id, count in _SPECIFIC_COUNTS.items()
    for ordinal in range(1, count + 1)
)

ACCEPTANCE_REGISTRY: dict[str, AcceptanceDefinition] = {
    item.acceptance_id: item for item in (*_SHARED, *_SPECIFIC)
}


def classify_acceptance(
    acceptance_id: str,
    *,
    evaluated: bool,
    expected_property_satisfied: bool | None,
    evidence_references: Iterable[str] = (),
    blocked_reason: str | None = None,
    observed_summary: str | None = None,
) -> AcceptanceResult:
    if acceptance_id not in ACCEPTANCE_REGISTRY:
        raise KeyError(f"UNKNOWN_ACCEPTANCE_ID:{acceptance_id}")
    evidence = tuple(evidence_references)
    if blocked_reason:
        return AcceptanceResult(
            acceptance_id,
            AcceptanceStatus.BLOCKED,
            blocked_reason,
            evidence,
            observed_summary,
        )
    if not evaluated:
        return AcceptanceResult(
            acceptance_id,
            AcceptanceStatus.BLOCKED,
            "NOT_EXECUTED",
            evidence,
            observed_summary,
        )
    if expected_property_satisfied is None or not evidence:
        return AcceptanceResult(
            acceptance_id,
            AcceptanceStatus.BLOCKED,
            "EVIDENCE_MISSING",
            evidence,
            observed_summary,
        )
    if expected_property_satisfied:
        return AcceptanceResult(
            acceptance_id,
            AcceptanceStatus.PASS,
            "EXPECTED_PROPERTY_PROVED",
            evidence,
            observed_summary,
        )
    return AcceptanceResult(
        acceptance_id,
        AcceptanceStatus.FAIL,
        "EXPECTED_PROPERTY_VIOLATED",
        evidence,
        observed_summary,
    )


def validate_acceptance_registry() -> None:
    if len(ACCEPTANCE_REGISTRY) != 128:
        raise ValueError("ACCEPTANCE_REGISTRY_MUST_CONTAIN_128_UNIQUE_IDS")
    expected_pe = {f"PE-{number:03d}" for number in range(1, 19)}
    mapped_pe = {pe_id for item in ACCEPTANCE_REGISTRY.values() for pe_id in item.pe_ids}
    if not expected_pe <= mapped_pe:
        raise ValueError("ACCEPTANCE_REGISTRY_PE_COVERAGE_INCOMPLETE")
