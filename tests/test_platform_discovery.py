from __future__ import annotations

from datetime import UTC, datetime, timedelta

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.discovery import (
    DiscoveryEngine,
    DiscoveryKind,
    DiscoveryProbe,
    stale_dependency_probe,
)
from attention_router.platform.findings import FindingCategory


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_discovery_reports_source_runtime_drift_without_remediation() -> None:
    engine = DiscoveryEngine()
    result = engine.evaluate(
        DiscoveryProbe(
            tenant_id=DEFAULT_TENANT_ID,
            kind=DiscoveryKind.SOURCE_RUNTIME_DRIFT,
            component_key="api",
            expected="source-a",
            observed="runtime-b",
            observed_at=NOW,
            freshness_expires_at=NOW + timedelta(seconds=30),
            source_revision="source-a",
            runtime_revision="runtime-b",
        ),
        now=NOW,
    )

    assert result.drift_detected is True
    assert result.observation.status == "DRIFT"
    assert result.finding_candidate is not None
    assert result.finding_candidate.category is FindingCategory.BLOCKER
    assert not hasattr(engine, "remediate")


def test_discovery_match_is_observation_only() -> None:
    result = DiscoveryEngine().evaluate(
        DiscoveryProbe(
            tenant_id=DEFAULT_TENANT_ID,
            kind=DiscoveryKind.TOPOLOGY_DRIFT,
            component_key="worker",
            expected="graph-v1",
            observed="graph-v1",
            observed_at=NOW,
            freshness_expires_at=NOW + timedelta(seconds=30),
        ),
        now=NOW,
    )

    assert result.drift_detected is False
    assert result.reason_code == "DISCOVERY_MATCH"
    assert result.finding_candidate is None


def test_stale_dependency_becomes_finding_candidate() -> None:
    probe = stale_dependency_probe(
        tenant_id=DEFAULT_TENANT_ID,
        dependency_id="dependency-1",
        component_key="transport",
        last_observed_at=NOW - timedelta(seconds=31),
        stale_after=timedelta(seconds=30),
        now=NOW,
    )
    result = DiscoveryEngine().evaluate(probe, now=NOW)

    assert result.drift_detected is True
    assert result.observation.status == "STALE"
    assert result.finding_candidate is not None
