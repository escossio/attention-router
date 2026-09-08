from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
import uuid

from sqlalchemy.orm import Session

from attention_router.infrastructure.models import AssertionResultRow
from attention_router.platform.privacy import sanitize_metadata, sanitized_summary


class AssertionOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class AssertionBlocked(ValueError):
    def __init__(self, assertion_ids: Iterable[str]) -> None:
        self.assertion_ids = tuple(assertion_ids)
        super().__init__("BLOCKING_ASSERTION_NOT_PASS:" + ",".join(self.assertion_ids))


@dataclass(frozen=True, slots=True)
class AssertionDefinition:
    assertion_id: str
    version: int
    assertion_type: str
    expected_property: str
    evaluator: str
    blocking: bool = True

    def __post_init__(self) -> None:
        if not self.assertion_id or self.version < 1:
            raise ValueError("ASSERTION_ID_AND_VERSION_REQUIRED")
        if not self.expected_property or not self.evaluator:
            raise ValueError("ASSERTION_CONTRACT_INCOMPLETE")


@dataclass(frozen=True, slots=True)
class EvaluatedAssertion:
    definition: AssertionDefinition
    outcome: AssertionOutcome
    reason_code: str
    sanitized_actual_summary: str | None
    evidence_refs: tuple[str, ...]

    @property
    def permits_dispatch(self) -> bool:
        return not self.definition.blocking or self.outcome is AssertionOutcome.PASS


def evaluate_predicate(
    definition: AssertionDefinition,
    *,
    observed: Any,
    predicate: Callable[[Any], bool | None],
    sanitized_actual_summary: str | None = None,
    evidence_refs: Iterable[str] = (),
) -> EvaluatedAssertion:
    """Evaluate a deterministic predicate without persisting the raw observation."""

    try:
        evaluated = predicate(observed)
    except Exception:
        return EvaluatedAssertion(
            definition=definition,
            outcome=AssertionOutcome.UNKNOWN,
            reason_code="ASSERTION_EVALUATOR_ERROR",
            sanitized_actual_summary=None,
            evidence_refs=tuple(evidence_refs),
        )
    if evaluated is True:
        outcome = AssertionOutcome.PASS
        reason_code = "ASSERTION_SATISFIED"
    elif evaluated is False:
        outcome = AssertionOutcome.FAIL
        reason_code = "ASSERTION_VIOLATED"
    else:
        outcome = AssertionOutcome.UNKNOWN
        reason_code = "ASSERTION_INDETERMINATE"
    return EvaluatedAssertion(
        definition=definition,
        outcome=outcome,
        reason_code=reason_code,
        sanitized_actual_summary=sanitized_actual_summary,
        evidence_refs=tuple(evidence_refs),
    )


def not_evaluated(
    definition: AssertionDefinition,
    *,
    reason_code: str,
    evidence_refs: Iterable[str] = (),
) -> EvaluatedAssertion:
    return EvaluatedAssertion(
        definition=definition,
        outcome=AssertionOutcome.NOT_EVALUATED,
        reason_code=reason_code,
        sanitized_actual_summary=None,
        evidence_refs=tuple(evidence_refs),
    )


def require_blocking_assertions_pass(results: Iterable[EvaluatedAssertion]) -> None:
    blocked = [
        result.definition.assertion_id
        for result in results
        if result.definition.blocking and result.outcome is not AssertionOutcome.PASS
    ]
    if blocked:
        raise AssertionBlocked(blocked)


def append_assertion_result(
    session: Session,
    *,
    tenant_id: str,
    scenario_run_id: str,
    scenario_step_run_id: str | None,
    evaluated: EvaluatedAssertion,
    attempt: int,
    provenance: Mapping[str, Any],
    finding_id: str | None = None,
    now: datetime | None = None,
) -> AssertionResultRow:
    """Append a sanitized result; callers cannot update a previous result."""

    observed_summary = sanitized_summary(
        evaluated.sanitized_actual_summary,
        maximum_length=1000,
    )
    row = AssertionResultRow(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        scenario_run_id=scenario_run_id,
        scenario_step_run_id=scenario_step_run_id,
        assertion_id=evaluated.definition.assertion_id,
        assertion_version=evaluated.definition.version,
        attempt=attempt,
        result=evaluated.outcome.value,
        expected_property=evaluated.definition.expected_property,
        observed_summary=observed_summary,
        evaluator=evaluated.definition.evaluator,
        blocking=evaluated.definition.blocking,
        evidence_reference_ids=list(evaluated.evidence_refs),
        finding_id=finding_id,
        evaluated_at=now or datetime.now(UTC),
        provenance=sanitize_metadata(provenance).value,
    )
    session.add(row)
    session.flush()
    return row
