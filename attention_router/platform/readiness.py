from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Iterable


class ReadinessDimension(StrEnum):
    COMPONENT_HEALTH = "COMPONENT_HEALTH"
    DOMAIN_READINESS = "DOMAIN_READINESS"
    EVIDENCE_READINESS = "EVIDENCE_READINESS"


class ReadinessState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


class DependencyRelation(StrEnum):
    MANDATORY = "MANDATORY"
    ADVISORY = "ADVISORY"


@dataclass(frozen=True, slots=True)
class DependencySignal:
    dependency_id: str
    state: ReadinessState
    relation: DependencyRelation
    observed_at: datetime
    freshness_expires_at: datetime
    reason_code: str
    evidence_only: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessEvaluation:
    dimension: ReadinessDimension
    subject_type: str
    subject_key: str
    state: ReadinessState
    reason_codes: tuple[str, ...]
    evaluated_at: datetime
    evidence_fresh_until: datetime | None
    required_dependency_ids: tuple[str, ...]
    blocker_references: tuple[str, ...]

    @property
    def permits_external_effect(self) -> bool:
        return self.dimension is ReadinessDimension.DOMAIN_READINESS and self.state is ReadinessState.READY


@dataclass(frozen=True, slots=True)
class ReadinessBundle:
    component: ReadinessEvaluation
    domain: ReadinessEvaluation
    evidence: ReadinessEvaluation


class ReadinessDenied(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


_BLOCKING_PRECEDENCE = (
    ReadinessState.BLOCKED,
    ReadinessState.UNKNOWN,
    ReadinessState.STALE,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def effective_signal_state(
    signal: DependencySignal,
    *,
    now: datetime,
    max_clock_skew: timedelta = timedelta(seconds=5),
) -> ReadinessState:
    timestamp = _utc(now)
    observed = _utc(signal.observed_at)
    fresh_until = _utc(signal.freshness_expires_at)
    if observed - timestamp > max_clock_skew:
        return ReadinessState.UNKNOWN
    if timestamp > fresh_until:
        return ReadinessState.STALE
    return signal.state


def evaluate_dimension(
    dimension: ReadinessDimension,
    *,
    subject_type: str,
    subject_key: str,
    signals: Iterable[DependencySignal],
    now: datetime | None = None,
    max_clock_skew: timedelta = timedelta(seconds=5),
) -> ReadinessEvaluation:
    timestamp = _utc(now or datetime.now(UTC))
    selected = tuple(
        signal
        for signal in signals
        if dimension is ReadinessDimension.EVIDENCE_READINESS or not signal.evidence_only
    )
    if dimension is ReadinessDimension.EVIDENCE_READINESS:
        evidence_signals = tuple(signal for signal in selected if signal.evidence_only)
        selected = evidence_signals or selected
    if not selected:
        return ReadinessEvaluation(
            dimension=dimension,
            subject_type=subject_type,
            subject_key=subject_key,
            state=ReadinessState.UNKNOWN,
            reason_codes=("READINESS_NO_SIGNALS",),
            evaluated_at=timestamp,
            evidence_fresh_until=None,
            required_dependency_ids=(),
            blocker_references=(),
        )

    evaluated = tuple(
        (signal, effective_signal_state(signal, now=timestamp, max_clock_skew=max_clock_skew))
        for signal in selected
    )
    mandatory = tuple(item for item in evaluated if item[0].relation is DependencyRelation.MANDATORY)
    blocker_references: tuple[str, ...] = ()
    state = ReadinessState.READY
    for blocking_state in _BLOCKING_PRECEDENCE:
        blockers = tuple(signal.dependency_id for signal, value in mandatory if value is blocking_state)
        if blockers:
            state = blocking_state
            blocker_references = blockers
            break
    else:
        if any(value is ReadinessState.DEGRADED for _, value in mandatory):
            state = ReadinessState.DEGRADED
        elif any(value is not ReadinessState.READY for _, value in evaluated):
            state = ReadinessState.DEGRADED

    reasons = tuple(
        sorted(
            {
                f"{signal.dependency_id}:{value.value}:{signal.reason_code}"
                for signal, value in evaluated
                if value is not ReadinessState.READY
            }
        )
    ) or ("READINESS_ALL_REQUIRED_SIGNALS_READY",)
    return ReadinessEvaluation(
        dimension=dimension,
        subject_type=subject_type,
        subject_key=subject_key,
        state=state,
        reason_codes=reasons,
        evaluated_at=timestamp,
        evidence_fresh_until=min(_utc(signal.freshness_expires_at) for signal in selected),
        required_dependency_ids=tuple(
            sorted(signal.dependency_id for signal in selected if signal.relation is DependencyRelation.MANDATORY)
        ),
        blocker_references=tuple(sorted(blocker_references)),
    )


def evaluate_readiness_bundle(
    *,
    subject_type: str,
    subject_key: str,
    signals: Iterable[DependencySignal],
    now: datetime | None = None,
    max_clock_skew: timedelta = timedelta(seconds=5),
) -> ReadinessBundle:
    signal_set = tuple(signals)
    timestamp = now or datetime.now(UTC)
    arguments = {
        "subject_type": subject_type,
        "subject_key": subject_key,
        "signals": signal_set,
        "now": timestamp,
        "max_clock_skew": max_clock_skew,
    }
    return ReadinessBundle(
        component=evaluate_dimension(ReadinessDimension.COMPONENT_HEALTH, **arguments),
        domain=evaluate_dimension(ReadinessDimension.DOMAIN_READINESS, **arguments),
        evidence=evaluate_dimension(ReadinessDimension.EVIDENCE_READINESS, **arguments),
    )


def require_dispatch_readiness(
    bundle: ReadinessBundle,
    *,
    evidence_required: bool = False,
    now: datetime | None = None,
    max_result_age: timedelta = timedelta(seconds=15),
) -> None:
    """Revalidate readiness at dispatch; arming-time eligibility is insufficient."""

    timestamp = _utc(now or datetime.now(UTC))
    if timestamp - _utc(bundle.domain.evaluated_at) > max_result_age:
        raise ReadinessDenied("READINESS_RESULT_STALE_AT_DISPATCH")
    if bundle.domain.state is not ReadinessState.READY:
        raise ReadinessDenied(f"DOMAIN_READINESS_{bundle.domain.state.value}_DENIES")
    if (
        bundle.domain.evidence_fresh_until is None
        or timestamp > _utc(bundle.domain.evidence_fresh_until)
    ):
        raise ReadinessDenied("READINESS_EVIDENCE_STALE_AT_DISPATCH")
    if bundle.component.state in {
        ReadinessState.BLOCKED,
        ReadinessState.UNKNOWN,
        ReadinessState.STALE,
    }:
        raise ReadinessDenied(f"COMPONENT_HEALTH_{bundle.component.state.value}_DENIES")
    if evidence_required and bundle.evidence.state is not ReadinessState.READY:
        raise ReadinessDenied(f"EVIDENCE_READINESS_{bundle.evidence.state.value}_DENIES")
