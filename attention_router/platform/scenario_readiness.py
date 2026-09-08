"""Generic, read-only scenario readiness orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.infrastructure.repository import audit
from attention_router.platform.operations import persist_readiness_result
from attention_router.platform.readiness import (
    DependencyRelation,
    DependencySignal,
    ReadinessBundle,
    ReadinessEvaluation,
    ReadinessState,
    evaluate_readiness_bundle,
)
from attention_router.platform.scenarios import ScenarioManifest


class ScenarioReadinessDenied(PermissionError):
    """Raised when an administrative readiness persistence is not authorized."""


@dataclass(frozen=True, slots=True)
class ScenarioReadinessContext:
    tenant_id: str
    scenario_id: str
    scenario_version: int
    now: datetime
    signals: Mapping[str, DependencySignal]
    services: Mapping[str, Any] = field(default_factory=dict)


def readiness_horizon(context: ScenarioReadinessContext) -> datetime:
    """Return the canonical maximum validity horizon for this evaluation."""
    return context.now + timedelta(seconds=settings.readiness_result_max_age)


ReadinessResolver = Callable[[ScenarioReadinessContext, str], DependencySignal]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _signal_from_context(context: ScenarioReadinessContext, requirement: str) -> DependencySignal:
    signal = context.signals.get(requirement)
    if signal is not None:
        return signal
    return DependencySignal(
        dependency_id=requirement,
        state=ReadinessState.UNKNOWN,
        relation=DependencyRelation.MANDATORY,
        observed_at=context.now,
        freshness_expires_at=context.now,
        reason_code="READINESS_RESOLVER_UNAVAILABLE",
    )


DEFAULT_READINESS_RESOLVERS: dict[str, ReadinessResolver] = {}


@dataclass(frozen=True, slots=True)
class ScenarioReadinessEvaluation:
    manifest: ScenarioManifest
    bundle: ReadinessBundle
    domain: ReadinessEvaluation
    requirements: tuple[str, ...]
    evidence: tuple[DependencySignal, ...]


def _requirements(manifest: ScenarioManifest) -> tuple[str, ...]:
    declared = manifest.readiness_requirements or manifest.preconditions
    return tuple(dict.fromkeys(declared))


def evaluate_scenario_readiness(
    *,
    manifest: ScenarioManifest,
    tenant_id: str,
    signals: Mapping[str, DependencySignal],
    resolvers: Mapping[str, ReadinessResolver] | None = None,
    now: datetime | None = None,
) -> ScenarioReadinessEvaluation:
    """Resolve declared prerequisites and evaluate them without persistence."""
    timestamp = _utc(now or datetime.now(UTC))
    context = ScenarioReadinessContext(
        tenant_id=tenant_id,
        scenario_id=manifest.scenario.id,
        scenario_version=manifest.scenario.version,
        now=timestamp,
        signals=signals,
    )
    registry = dict(DEFAULT_READINESS_RESOLVERS)
    registry.update(resolvers or {})
    evidence = tuple(
        registry.get(requirement, _signal_from_context)(context, requirement)
        for requirement in _requirements(manifest)
    )
    bundle = evaluate_readiness_bundle(
        subject_type="SCENARIO",
        subject_key=f"{manifest.scenario.id}:v{manifest.scenario.version}",
        signals=evidence,
        now=timestamp,
    )
    return ScenarioReadinessEvaluation(
        manifest=manifest,
        bundle=bundle,
        domain=bundle.domain,
        requirements=_requirements(manifest),
        evidence=evidence,
    )


def persist_scenario_readiness(
    session: Session,
    *,
    evaluation: ScenarioReadinessEvaluation,
    tenant_id: str,
    requested_by: str,
    source_references: tuple[str, ...] = (),
) -> object:
    """Persist one evaluated result; never creates execution state."""
    if not requested_by or requested_by in {"SYNTHETIC_ACTOR", "ANDY", "PROVIDER", "TRANSPORT"}:
        raise ScenarioReadinessDenied("READINESS_PERSIST_AUTHORITY_DENIED")
    row = persist_readiness_result(
        session,
        tenant_id=tenant_id,
        evaluation=evaluation.domain,
        source_references=source_references or evaluation.requirements,
        provenance={
            "scenario_id": evaluation.manifest.scenario.id,
            "scenario_version": evaluation.manifest.scenario.version,
            "requested_by": requested_by,
            "evidence_count": len(evaluation.evidence),
        },
    )
    # Keep the rich in-memory evaluation available to the handoff diagnostic;
    # this is deliberately transient and is not persisted or queried later.
    row._readiness_evaluation = evaluation
    audit(
        session,
        None,
        "scenario_readiness_evaluated",
        {
            "scenario_id": evaluation.manifest.scenario.id,
            "scenario_version": evaluation.manifest.scenario.version,
            "readiness_result_id": row.id,
            "state": row.state,
            "requested_by": requested_by,
            "evidence_count": len(evaluation.evidence),
        },
        tenant_id=tenant_id,
    )
    session.flush()
    return row


def evaluate_and_persist_scenario_readiness(
    session: Session,
    *,
    manifest: ScenarioManifest,
    tenant_id: str,
    signals: Mapping[str, DependencySignal],
    requested_by: str,
    resolvers: Mapping[str, ReadinessResolver] | None = None,
    source_references: tuple[str, ...] = (),
    now: datetime | None = None,
) -> tuple[ScenarioReadinessEvaluation, object]:
    """Administrative entrypoint: evaluate once, then persist once."""
    evaluation = evaluate_scenario_readiness(
        manifest=manifest,
        tenant_id=tenant_id,
        signals=signals,
        resolvers=resolvers,
        now=now,
    )
    row = persist_scenario_readiness(
        session,
        evaluation=evaluation,
        tenant_id=tenant_id,
        requested_by=requested_by,
        source_references=source_references,
    )
    return evaluation, row
