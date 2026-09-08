from datetime import UTC, datetime, timedelta
from pathlib import Path

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.readiness import DependencyRelation, DependencySignal, ReadinessState
from attention_router.platform.live_readiness import (
    collect_live_readiness_signals,
    evaluate_live_scenario_readiness,
)
from attention_router.platform.scenarios import load_manifest_file
from attention_router.platform.scenario_readiness import ScenarioReadinessContext
from attention_router.platform.live_readiness_adapters import _signal
from attention_router.application.execution import TransportStatusSnapshot


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class ReadySource:
    def resolve(self, context, requirement):
        return DependencySignal(
            dependency_id=requirement,
            state=ReadinessState.READY,
            relation=DependencyRelation.MANDATORY,
            observed_at=context.now,
            freshness_expires_at=context.now + timedelta(minutes=5),
            reason_code="OFFICIAL_SOURCE_READY",
        )


class FailingSource:
    def resolve(self, context, requirement):
        raise RuntimeError("source unavailable")


def test_live_collector_resolves_declared_requirements_without_manual_outcomes():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    sources = {requirement: ReadySource() for requirement in manifest.preconditions}
    result = evaluate_live_scenario_readiness(
        tenant_id=DEFAULT_TENANT_ID, manifest=manifest, sources=sources, now=NOW
    )
    assert result.requirements == manifest.preconditions
    assert result.domain.state is ReadinessState.READY


def test_missing_live_resolver_fails_closed():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    result = evaluate_live_scenario_readiness(
        tenant_id=DEFAULT_TENANT_ID, manifest=manifest, sources={}, now=NOW
    )
    assert result.domain.state is ReadinessState.UNKNOWN
    assert any("READINESS_RESOLVER_MISSING" in reason for reason in result.domain.reason_codes)


def test_live_resolver_exception_fails_closed():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    sources = {requirement: FailingSource() for requirement in manifest.preconditions}
    result = evaluate_live_scenario_readiness(
        tenant_id=DEFAULT_TENANT_ID, manifest=manifest, sources=sources, now=NOW
    )
    assert result.domain.state is ReadinessState.UNKNOWN
    assert any("READINESS_RESOLVER_ERROR" in reason for reason in result.domain.reason_codes)


def test_default_collector_wires_configured_synthetic_transport_source(monkeypatch):
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    monkeypatch.setattr("attention_router.config.settings.synthetic_transport_status_url", "http://synthetic/status")
    monkeypatch.setattr(
        "attention_router.application.execution.get_transport_status",
        lambda **kwargs: TransportStatusSnapshot(
            "synthetic", "synthetic", "READY", True, True, True, True,
            NOW, NOW + timedelta(minutes=5), "TRANSPORT_READY", "transport:synthetic",
        ),
    )
    collection = collect_live_readiness_signals(
        tenant_id=DEFAULT_TENANT_ID, manifest=manifest, now=NOW,
    )
    signal = collection.signals["SYNTHETIC_WHATSAPP_READY"]
    assert signal.state is ReadinessState.READY
    assert signal.reason_code == "TRANSPORT_READY"


def _signal_context():
    return ScenarioReadinessContext(DEFAULT_TENANT_ID, "SCN-PE-029", 1, NOW, {})


def test_instant_signal_missing_expiry_inherits_canonical_readiness_horizon():
    signal = _signal(_signal_context(), "INSTANT", ReadinessState.READY, "READY")
    assert signal.freshness_expires_at == NOW + timedelta(seconds=15)


def test_signal_expiry_is_bounded_without_extending_short_sources():
    context = _signal_context()
    short = _signal(context, "SHORT", ReadinessState.READY, "READY", until=NOW + timedelta(seconds=4))
    long = _signal(context, "LONG", ReadinessState.READY, "READY", until=NOW + timedelta(seconds=60))
    equal = _signal(context, "EQUAL", ReadinessState.READY, "READY", until=NOW + timedelta(seconds=15))
    expired = _signal(context, "EXPIRED", ReadinessState.STALE, "STALE", until=NOW - timedelta(seconds=1))
    explicit_zero = _signal(context, "ZERO", ReadinessState.STALE, "STALE", until=NOW)
    assert short.freshness_expires_at == NOW + timedelta(seconds=4)
    assert long.freshness_expires_at == NOW + timedelta(seconds=15)
    assert equal.freshness_expires_at == NOW + timedelta(seconds=15)
    assert expired.freshness_expires_at == NOW - timedelta(seconds=1)
    assert explicit_zero.freshness_expires_at == NOW
