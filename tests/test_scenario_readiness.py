from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import ReadinessResultRow
from attention_router.platform.readiness import (
    DependencyRelation,
    DependencySignal,
    ReadinessState,
)
from attention_router.platform.scenario_readiness import (
    ScenarioReadinessDenied,
    evaluate_and_persist_scenario_readiness,
    evaluate_scenario_readiness,
)
from attention_router.platform.scenarios import load_manifest_file


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _signals(manifest, *, stale: str | None = None):
    return {
        requirement: DependencySignal(
            dependency_id=requirement,
            state=ReadinessState.STALE if requirement == stale else ReadinessState.READY,
            relation=DependencyRelation.MANDATORY,
            observed_at=NOW - timedelta(seconds=1),
            freshness_expires_at=(NOW - timedelta(seconds=1) if requirement == stale else NOW + timedelta(minutes=5)),
            reason_code="FIXTURE",
        )
        for requirement in manifest.preconditions
    }


def test_canonical_scenario_readiness_is_generic_and_pure():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    result = evaluate_scenario_readiness(
        manifest=manifest,
        tenant_id=DEFAULT_TENANT_ID,
        signals=_signals(manifest),
        now=NOW,
    )
    assert result.domain.state is ReadinessState.READY
    assert result.domain.evidence_fresh_until == NOW + timedelta(minutes=5)


def test_stale_mandatory_prerequisite_fails_closed():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    result = evaluate_scenario_readiness(
        manifest=manifest,
        tenant_id=DEFAULT_TENANT_ID,
        signals=_signals(manifest, stale="OWNER_PRESENCE_FRESH"),
        now=NOW,
    )
    assert result.domain.state is ReadinessState.STALE


def test_admin_persistence_creates_distinct_history_and_requires_authority(session):
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    first, first_row = evaluate_and_persist_scenario_readiness(
        session,
        manifest=manifest,
        tenant_id=DEFAULT_TENANT_ID,
        signals=_signals(manifest),
        requested_by="operator:readiness-review",
        now=NOW,
    )
    second, second_row = evaluate_and_persist_scenario_readiness(
        session,
        manifest=manifest,
        tenant_id=DEFAULT_TENANT_ID,
        signals=_signals(manifest, stale="OWNER_PRESENCE_FRESH"),
        requested_by="operator:readiness-review",
        now=NOW + timedelta(minutes=1),
    )
    assert first.domain.state is ReadinessState.READY
    assert second.domain.state is ReadinessState.STALE
    assert first_row.id != second_row.id
    assert first_row.is_current is False
    assert second_row.is_current is True
    assert session.scalar(select(ReadinessResultRow).where(ReadinessResultRow.id == first_row.id)).state == "READY"
    with pytest.raises(ScenarioReadinessDenied):
        evaluate_and_persist_scenario_readiness(
            session,
            manifest=manifest,
            tenant_id=DEFAULT_TENANT_ID,
            signals=_signals(manifest),
            requested_by="ANDY",
            now=NOW,
        )
