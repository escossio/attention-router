from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from attention_router.platform.scenarios import ScenarioTestLevel, load_manifest_file
from attention_router.platform.synthetic_driver import (
    DriverReason,
    SyntheticActorContract,
    SyntheticDriverConfig,
    SyntheticDriverV1,
)
from attention_router.platform.bounded_authorization import create_bounded_authorization


ROOT = Path("config/platform/scenarios")


def test_synthetic_actor_is_structurally_not_owner_and_not_arbitrary():
    actor = SyntheticActorContract()
    actor.validate()
    assert actor.is_owner is False
    assert actor.inherits_owner_authority is False
    assert actor.can_send_arbitrary_target is False


def test_l1_is_blocked_by_default_before_external_effect():
    driver = SyntheticDriverV1(manifest_root=ROOT)
    manifest = load_manifest_file(ROOT / "SCN-PE-011.yaml")
    result = driver.preflight(manifest)
    assert result.ready is False
    assert DriverReason.L1_NOT_AUTHORIZED.value in result.reasons
    assert DriverReason.EXTERNAL_SEND_NOT_AUTHORIZED.value in result.reasons


def test_l0_manifest_is_allowed_but_l1_run_is_fail_closed():
    driver = SyntheticDriverV1(
        config=SyntheticDriverConfig(max_allowed_level=ScenarioTestLevel.L0_UNIT_DRY),
        manifest_root=ROOT,
    )
    l0 = driver.load("SCN-PE-001")
    assert l0.scenario.level is ScenarioTestLevel.L0_UNIT_DRY
    l1 = driver.load("SCN-PE-011")
    assert driver.preflight(l1).ready is False


def test_external_hook_cannot_become_generic_send_endpoint():
    driver = SyntheticDriverV1(
        config=SyntheticDriverConfig(
            external_send_authorized=True,
            whatsapp_ready=True,
            target_allowlist=frozenset({"SYNTHETIC_TEST_ACTOR"}),
            max_external_messages=1,
        ),
        manifest_root=ROOT,
    )
    with pytest.raises(PermissionError, match="PLATFORM_EXECUTION_AUTHORIZATION"):
        driver.authorize_external_send(
            run_id="run-test", target_ref="SYNTHETIC_TEST_ACTOR", budget=1
        )


def test_unknown_scenario_fails_closed():
    with pytest.raises(FileNotFoundError, match="SCENARIO_NOT_FOUND"):
        SyntheticDriverV1(manifest_root=ROOT).load("SCN-PE-999")


def test_driver_uses_persisted_bounded_authorization_before_l1_admission(session):
    driver = SyntheticDriverV1(manifest_root=ROOT)
    scope = dict(
        tenant_id="tenant-1", scenario_run_id="run-1", effect_budget_id="budget-1",
        level="L1_SYNTHETIC_E2E", actor_scope="actor-1", target_scope="target-1",
        capability_scope="conversation.reply", effect_scope="effect-1", max_effects=1,
        authorized_by="HUMAN_OPERATOR", correlation_id="corr-1",
        valid_from=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    denied = driver.check_bounded_authorization(session, **{
        key: scope[key] for key in (
            "tenant_id", "scenario_run_id", "effect_budget_id", "level", "actor_scope",
            "target_scope", "capability_scope", "effect_scope",
        )
    })
    assert denied.allowed is False
    create_bounded_authorization(session, **scope)
    session.commit()
    allowed = driver.check_bounded_authorization(session, **{
        key: scope[key] for key in (
            "tenant_id", "scenario_run_id", "effect_budget_id", "level", "actor_scope",
            "target_scope", "capability_scope", "effect_scope",
        )
    })
    assert allowed.allowed is True
    assert driver.preflight(driver.load("SCN-PE-011")).external_send_authorized is False
