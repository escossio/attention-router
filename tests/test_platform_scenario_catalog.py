from pathlib import Path

from attention_router.platform.scenarios import (
    EffectClass,
    ScenarioTestLevel,
    load_manifest_file,
)


CATALOG = Path(__file__).parents[1] / "config" / "platform" / "scenarios"


def test_catalog_loads_exactly_twenty_eight_unique_immutable_v1_manifests():
    paths = sorted(CATALOG.glob("SCN-PE-*.yaml"))
    manifests = [load_manifest_file(path) for path in paths]
    assert len(manifests) == 29
    assert len({manifest.scenario.id for manifest in manifests}) == 29
    assert {manifest.scenario.id for manifest in manifests} == {
        f"SCN-PE-{number:03d}" for number in range(1, 30)
    }
    assert all(manifest.scenario.version == 1 for manifest in manifests)
    assert all(len(manifest.content_hash()) == 64 for manifest in manifests)


def test_only_driver_restart_is_l1_and_it_is_bounded_one_plus_one():
    manifests = [load_manifest_file(path) for path in CATALOG.glob("SCN-PE-*.yaml")]
    external = [
        manifest
        for manifest in manifests
        if manifest.scenario.effect_class is EffectClass.SYNTHETIC_EXTERNAL_EFFECT
    ]
    assert sorted(manifest.scenario.id for manifest in external) == ["SCN-PE-011", "SCN-PE-029"]
    manifest = external[0]
    assert manifest.scenario.level is ScenarioTestLevel.L1_SYNTHETIC_E2E
    assert manifest.safety.effect_budget.max_stimulus == 1
    assert manifest.safety.effect_budget.max_system_response == 1
    assert manifest.safety.retry.external_effect_blind_retries == 0


def test_all_l0_scenarios_have_zero_external_effect_budget():
    manifests = [load_manifest_file(path) for path in CATALOG.glob("SCN-PE-*.yaml")]
    l0 = [manifest for manifest in manifests if manifest.scenario.level is ScenarioTestLevel.L0_UNIT_DRY]
    assert len(l0) == 27
    assert all(manifest.safety.effect_budget.max_stimulus == 0 for manifest in l0)
    assert all(manifest.safety.effect_budget.max_system_response == 0 for manifest in l0)
    assert all(not manifest.safety.allowed_target_scope for manifest in l0)
