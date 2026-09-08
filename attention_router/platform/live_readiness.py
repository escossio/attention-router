"""Read-only collection of live prerequisite signals for scenario readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from attention_router.platform.readiness import (
    DependencyRelation,
    DependencySignal,
    ReadinessState,
)
from attention_router.platform.scenario_readiness import (
    ScenarioReadinessContext,
    ScenarioReadinessEvaluation,
    evaluate_scenario_readiness,
)
from attention_router.platform.scenarios import ScenarioManifest


class LiveReadinessSource(Protocol):
    """Read-only source adapter; implementations must not issue commands."""

    def resolve(self, context: ScenarioReadinessContext, requirement: str) -> DependencySignal: ...


@dataclass(frozen=True, slots=True)
class LiveReadinessCollection:
    context: ScenarioReadinessContext
    requirements: tuple[str, ...]
    signals: Mapping[str, DependencySignal]


def _unknown(context: ScenarioReadinessContext, requirement: str, reason: str) -> DependencySignal:
    return DependencySignal(
        dependency_id=requirement,
        state=ReadinessState.UNKNOWN,
        relation=DependencyRelation.MANDATORY,
        observed_at=context.now,
        freshness_expires_at=context.now,
        reason_code=reason,
    )


def collect_live_readiness_signals(
    *,
    tenant_id: str,
    manifest: ScenarioManifest,
    sources: Mapping[str, LiveReadinessSource] | None = None,
    services: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> LiveReadinessCollection:
    """Collect declared prerequisites from read-only official source adapters."""
    timestamp = now or datetime.now(UTC)
    context = ScenarioReadinessContext(
        tenant_id=tenant_id,
        scenario_id=manifest.scenario.id,
        scenario_version=manifest.scenario.version,
        now=timestamp,
        signals={},
        services=services or {},
    )
    # Resolve shared read-only domain snapshots once per evaluation.
    if "session" in context.services:
        from attention_router.application.platform.pre_run_context import resolve_pre_run_execution_context
        from attention_router.platform.execution_contract import get_execution_runtime_contract
        shared = dict(context.services)
        shared.setdefault("pre_run_context", resolve_pre_run_execution_context(
            context.services["session"], tenant_id=tenant_id, scenario_id=manifest.scenario.id,
            scenario_version=manifest.scenario.version,
            synthetic_actor_key=str(context.services.get("synthetic_actor_key", "actor_synthetic_test_actor")),
            now=timestamp,
        ))
        shared.setdefault("execution_runtime_contract", get_execution_runtime_contract())
        context = ScenarioReadinessContext(
            tenant_id=context.tenant_id, scenario_id=context.scenario_id,
            scenario_version=context.scenario_version, now=context.now,
            signals=context.signals, services=shared,
        )
        # Disclosure sources are resolved by the canonical adapter from the
        # persisted policy and represented-subject state.  Do not accept
        # caller-supplied disclosure context on the live path.
    if sources is None:
        from attention_router.platform.live_readiness_adapters import build_default_live_readiness_adapters
        sources = build_default_live_readiness_adapters()
    if "synthetic_transport_status" not in context.services:
        from attention_router.application.execution import get_transport_status
        from attention_router.config import settings
        configured = settings.synthetic_transport_status_url
        if configured:
            context = ScenarioReadinessContext(
                tenant_id=context.tenant_id, scenario_id=context.scenario_id,
                scenario_version=context.scenario_version, now=context.now,
                signals=context.signals,
                services={**context.services, "synthetic_transport_status": lambda: get_transport_status(
                    transport_id="synthetic", role="synthetic", status_url=configured,
                    timeout=settings.synthetic_transport_status_timeout_seconds,
                )},
            )
    requirements = tuple(dict.fromkeys(manifest.readiness_requirements or manifest.preconditions))
    signals: dict[str, DependencySignal] = {}
    for requirement in requirements:
        source = sources.get(requirement)
        if source is None:
            signals[requirement] = _unknown(context, requirement, "READINESS_RESOLVER_MISSING")
            continue
        try:
            signal = source.resolve(context, requirement)
        except Exception:
            signal = _unknown(context, requirement, "READINESS_RESOLVER_ERROR")
        signals[requirement] = signal
    return LiveReadinessCollection(context=context, requirements=requirements, signals=signals)


def evaluate_live_scenario_readiness(
    *,
    tenant_id: str,
    manifest: ScenarioManifest,
    sources: Mapping[str, LiveReadinessSource] | None = None,
    services: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ScenarioReadinessEvaluation:
    """Collect live evidence, then delegate evaluation to the canonical orchestrator."""
    collection = collect_live_readiness_signals(
        tenant_id=tenant_id, manifest=manifest, sources=sources, services=services, now=now
    )
    return evaluate_scenario_readiness(
        manifest=manifest,
        tenant_id=tenant_id,
        signals=collection.signals,
        now=collection.context.now,
    )
