from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from collections.abc import Iterable, Mapping
from typing import Any
import uuid

from sqlalchemy.orm import Session

from attention_router.infrastructure.models import InvariantResultRow
from attention_router.platform.privacy import sanitize_metadata, sanitized_summary


class EnforcementMode(StrEnum):
    NATIVE_ENFORCEMENT = "NATIVE_ENFORCEMENT"
    EVIDENCE_ADAPTER = "EVIDENCE_ADAPTER"
    SCENARIO_EVALUATOR = "SCENARIO_EVALUATOR"


class InvariantOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class InvariantSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class InvariantSpec:
    invariant_id: str
    owner: str
    enforcement_mode: EnforcementMode
    severity: InvariantSeverity
    blocking: bool


@dataclass(frozen=True, slots=True)
class EvaluatedInvariant:
    invariant_id: str
    outcome: InvariantOutcome
    reason_code: str
    evidence_refs: tuple[str, ...] = ()


class InvariantBlocked(ValueError):
    def __init__(self, invariant_ids: Iterable[str]) -> None:
        self.invariant_ids = tuple(invariant_ids)
        super().__init__("BLOCKING_INVARIANT_NOT_PASS:" + ",".join(self.invariant_ids))


_FAMILIES: dict[str, tuple[tuple[str, ...], str, EnforcementMode, InvariantSeverity]] = {
    "AUTH": (
        ("001", "002", "003", "004"),
        "authority",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "READY": (
        ("001", "002", "003", "004"),
        "readiness",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.HIGH,
    ),
    "LEASE": (
        ("001", "002", "003", "004", "005", "006", "007"),
        "execution_safety",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "BUDGET": (
        ("001", "002", "003", "004"),
        "execution_safety",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "SEND": (
        ("001", "002", "003", "004"),
        "execution_safety",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "SCEN": (
        ("001", "002", "003", "004", "005", "006"),
        "scenario",
        EnforcementMode.SCENARIO_EVALUATOR,
        InvariantSeverity.CRITICAL,
    ),
    "MEM": (
        ("001", "002"),
        "lineage",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "FIND": (
        ("001", "002", "003", "004", "005"),
        "findings",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.HIGH,
    ),
    "RESTART": (
        ("001", "002"),
        "recovery",
        EnforcementMode.EVIDENCE_ADAPTER,
        InvariantSeverity.CRITICAL,
    ),
    "PROV": (
        ("001", "002"),
        "provenance",
        EnforcementMode.EVIDENCE_ADAPTER,
        InvariantSeverity.HIGH,
    ),
    "DIAG": (
        ("001",),
        "diagnosis",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.HIGH,
    ),
    "REMED": (
        ("001", "002"),
        "governance",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "PROM": (
        ("001", "002", "003"),
        "promotion",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "PRIV": (
        ("001",),
        "privacy",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "TENANT": (
        ("001", "002"),
        "tenancy",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
    "API": (
        ("001", "002"),
        "api",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.HIGH,
    ),
    "SAFETY": (
        ("001", "002", "003"),
        "safety",
        EnforcementMode.NATIVE_ENFORCEMENT,
        InvariantSeverity.CRITICAL,
    ),
}


def _canonical_specs() -> tuple[InvariantSpec, ...]:
    specs: list[InvariantSpec] = []
    for family, (numbers, owner, mode, severity) in _FAMILIES.items():
        specs.extend(
            InvariantSpec(
                invariant_id=f"INV-PE-{family}-{number}",
                owner=owner,
                enforcement_mode=mode,
                severity=severity,
                blocking=True,
            )
            for number in numbers
        )
    return tuple(specs)


CANONICAL_INVARIANTS = _canonical_specs()
CANONICAL_INVARIANT_IDS = frozenset(spec.invariant_id for spec in CANONICAL_INVARIANTS)

if len(CANONICAL_INVARIANTS) != 54 or len(CANONICAL_INVARIANT_IDS) != 54:
    raise RuntimeError("CANONICAL_INVARIANT_CARDINALITY_MUST_EQUAL_54")


class InvariantRegistry:
    def __init__(self, specs: Iterable[InvariantSpec] = CANONICAL_INVARIANTS) -> None:
        materialized = tuple(specs)
        self._specs = {spec.invariant_id: spec for spec in materialized}
        if len(self._specs) != len(materialized):
            raise ValueError("DUPLICATE_INVARIANT_ID")

    def get(self, invariant_id: str) -> InvariantSpec:
        try:
            return self._specs[invariant_id]
        except KeyError as exc:
            raise KeyError(f"UNKNOWN_INVARIANT:{invariant_id}") from exc

    def all(self) -> tuple[InvariantSpec, ...]:
        return tuple(self._specs[invariant_id] for invariant_id in sorted(self._specs))

    def require_canonical_coverage(self) -> None:
        actual = set(self._specs)
        if actual != CANONICAL_INVARIANT_IDS:
            missing = sorted(CANONICAL_INVARIANT_IDS - actual)
            extra = sorted(actual - CANONICAL_INVARIANT_IDS)
            raise ValueError(f"INVARIANT_COVERAGE_MISMATCH:missing={missing}:extra={extra}")

    def require_blocking_pass(self, results: Iterable[EvaluatedInvariant]) -> None:
        blocked: list[str] = []
        for result in results:
            spec = self.get(result.invariant_id)
            if spec.blocking and result.outcome is not InvariantOutcome.PASS:
                blocked.append(result.invariant_id)
        if blocked:
            raise InvariantBlocked(blocked)


def append_invariant_result(
    session: Session,
    *,
    registry: InvariantRegistry,
    tenant_id: str,
    scenario_run_id: str,
    scenario_step_run_id: str | None,
    invariant_id: str,
    outcome: InvariantOutcome,
    attempt: int,
    reason_code: str,
    evidence_refs: Iterable[str],
    provenance: Mapping[str, Any],
    finding_id: str | None = None,
    blocks_promotion: bool | None = None,
    now: datetime | None = None,
) -> InvariantResultRow:
    """Append evidence for native or scenario enforcement without suppressing it."""

    spec = registry.get(invariant_id)
    violated = outcome is InvariantOutcome.FAIL
    row = InvariantResultRow(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        scenario_run_id=scenario_run_id,
        scenario_step_run_id=scenario_step_run_id,
        invariant_id=invariant_id,
        attempt=attempt,
        result=outcome.value,
        enforcement_owner=spec.owner,
        severity=spec.severity.value,
        blocking=spec.blocking,
        violation_reason=(
            sanitized_summary(reason_code, maximum_length=320) if violated else None
        ),
        aborts_scenario=violated and spec.blocking,
        blocks_promotion=spec.blocking if blocks_promotion is None else blocks_promotion,
        evidence_reference_ids=list(evidence_refs),
        finding_id=finding_id,
        evaluated_at=now or datetime.now(UTC),
        provenance=sanitize_metadata(provenance).value,
    )
    session.add(row)
    session.flush()
    return row
