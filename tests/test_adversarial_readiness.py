from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from attention_router.platform.live_readiness import evaluate_live_scenario_readiness
from attention_router.platform.readiness import DependencyRelation, DependencySignal, ReadinessState
from attention_router.platform.scenarios import load_manifest_file

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


class Source:
    def __init__(self, state=ReadinessState.READY, expires=None):
        self.state = state
        self.expires = expires or NOW + timedelta(minutes=5)

    def resolve(self, context, requirement):
        return DependencySignal(requirement, self.state, DependencyRelation.MANDATORY, NOW, self.expires, "TEST")


@pytest.mark.parametrize("state", [ReadinessState.UNKNOWN, ReadinessState.BLOCKED, ReadinessState.STALE])
def test_any_mandatory_negative_signal_blocks_global_ready(state):
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    sources = {key: Source() for key in manifest.preconditions}
    sources[manifest.preconditions[0]] = Source(state)
    result = evaluate_live_scenario_readiness(tenant_id="tenant", manifest=manifest, sources=sources, now=NOW)
    assert result.domain.state is not ReadinessState.READY


def test_stale_mandatory_evidence_blocks_global_ready():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    sources = {key: Source() for key in manifest.preconditions}
    sources[manifest.preconditions[0]] = Source(expires=NOW - timedelta(seconds=1))
    result = evaluate_live_scenario_readiness(tenant_id="tenant", manifest=manifest, sources=sources, now=NOW)
    assert result.domain.state is not ReadinessState.READY
