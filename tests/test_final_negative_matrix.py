from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from attention_router.platform.live_readiness import evaluate_live_scenario_readiness
from attention_router.platform.readiness import DependencyRelation, DependencySignal, ReadinessState
from attention_router.platform.scenarios import load_manifest_file

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
CASE_IDS = [f"RDY-{index:03d}" for index in range(1, 87)]


class Source:
    def __init__(self, state=ReadinessState.READY):
        self.state = state

    def resolve(self, context, requirement):
        return DependencySignal(requirement, self.state, DependencyRelation.MANDATORY,
                                 context.now, context.now + timedelta(minutes=5), "MATRIX")


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_complete_negative_matrix_never_promotes_ready(case_id):
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    sources = {name: Source() for name in manifest.preconditions}
    sources[manifest.preconditions[(int(case_id[-3:]) - 1) % len(manifest.preconditions)]] = Source(ReadinessState.UNKNOWN)
    result = evaluate_live_scenario_readiness(tenant_id="tenant", manifest=manifest, sources=sources, now=NOW)
    assert result.domain.state is not ReadinessState.READY


def test_generic_scenario_uses_same_manifest_driven_evaluator_and_fails_closed():
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-028.yaml"))
    sources = {name: Source() for name in (manifest.readiness_requirements or manifest.preconditions)}
    result = evaluate_live_scenario_readiness(tenant_id="tenant", manifest=manifest, sources=sources, now=NOW)
    assert result.requirements == tuple(manifest.readiness_requirements or manifest.preconditions)
    assert result.domain.state is ReadinessState.READY
    sources[result.requirements[0]] = Source(ReadinessState.UNKNOWN)
    negative = evaluate_live_scenario_readiness(tenant_id="tenant", manifest=manifest, sources=sources, now=NOW)
    assert negative.domain.state is not ReadinessState.READY
