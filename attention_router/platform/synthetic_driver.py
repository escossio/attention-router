"""Safe control-plane for the Synthetic Test Driver.

The WhatsApp process is deliberately not an authority source.  This module
owns scenario contracts, preflight decisions and the L0 dry-run boundary;
external delivery remains opt-in and is rejected by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from attention_router.platform.bounded_authorization import (
    BoundedAuthorizationResult,
    check_bounded_authorization,
)
from attention_router.platform.l0_executor import L0RunResult, execute_l0_manifest
from attention_router.platform.scenarios import (
    EffectClass,
    ScenarioManifest,
    ScenarioTestLevel,
    load_manifest_file,
)


class DriverReason(StrEnum):
    L1_NOT_AUTHORIZED = "L1_NOT_AUTHORIZED"
    EXTERNAL_SEND_NOT_AUTHORIZED = "EXTERNAL_SEND_NOT_AUTHORIZED"
    SYNTHETIC_WHATSAPP_NOT_READY = "SYNTHETIC_WHATSAPP_NOT_READY"
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SCENARIO_NOT_FOUND = "SCENARIO_NOT_FOUND"
    BOUNDED_AUTHORIZATION_NOT_PROVED = "BOUNDED_AUTHORIZATION_NOT_PROVED"


@dataclass(frozen=True, slots=True)
class SyntheticActorContract:
    actor_key: str = "actor_synthetic_test_actor"
    category: str = "SYNTHETIC_TEST_ACTOR"
    is_owner: bool = False
    inherits_owner_authority: bool = False
    can_send_arbitrary_target: bool = False

    def validate(self) -> None:
        if self.is_owner or self.inherits_owner_authority:
            raise ValueError("SYNTHETIC_ACTOR_OWNER_AUTHORITY_FORBIDDEN")
        if self.category != "SYNTHETIC_TEST_ACTOR":
            raise ValueError("SYNTHETIC_ACTOR_CATEGORY_INVALID")
        if self.can_send_arbitrary_target:
            raise ValueError("SYNTHETIC_ARBITRARY_TARGET_FORBIDDEN")


@dataclass(frozen=True, slots=True)
class ScenarioPreflight:
    scenario_id: str
    level: ScenarioTestLevel
    ready: bool
    external_send_authorized: bool = False
    reasons: tuple[str, ...] = ()
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class SyntheticDriverConfig:
    max_allowed_level: ScenarioTestLevel = ScenarioTestLevel.L0_UNIT_DRY
    external_send_authorized: bool = False
    whatsapp_ready: bool = False
    target_allowlist: frozenset[str] = frozenset()
    max_external_messages: int = 0
    runtime_revision: str = "unknown"
    schema_revision: str = "unknown"
    bounded_authorization_ready: bool = False


@dataclass(frozen=True, slots=True)
class SyntheticL0Result:
    scenario_id: str
    status: str
    reason_code: str
    run_id: str | None = None
    evidence_ref: str | None = None


class SyntheticDriverV1:
    """Manifest-driven driver facade with a fail-closed external boundary."""

    def __init__(
        self,
        *,
        config: SyntheticDriverConfig | None = None,
        actor: SyntheticActorContract | None = None,
        manifest_root: Path = Path("config/platform/scenarios"),
    ) -> None:
        self.config = config or SyntheticDriverConfig()
        self.actor = actor or SyntheticActorContract()
        self.actor.validate()
        self.manifest_root = manifest_root

    def load(self, scenario_id: str) -> ScenarioManifest:
        path = self.manifest_root / f"{scenario_id}.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"{DriverReason.SCENARIO_NOT_FOUND}:{scenario_id}")
        manifest = load_manifest_file(path)
        if manifest.scenario.id != scenario_id:
            raise ValueError("SCENARIO_ID_PATH_MISMATCH")
        return manifest

    def preflight(self, manifest: ScenarioManifest) -> ScenarioPreflight:
        reasons: list[str] = []
        external = manifest.scenario.effect_class is not EffectClass.NO_EXTERNAL_EFFECT
        if manifest.scenario.level.value > self.config.max_allowed_level.value:
            reasons.append(DriverReason.L1_NOT_AUTHORIZED.value)
        if external and not self.config.external_send_authorized:
            reasons.append(DriverReason.EXTERNAL_SEND_NOT_AUTHORIZED.value)
        if external and not self.config.whatsapp_ready:
            reasons.append(DriverReason.SYNTHETIC_WHATSAPP_NOT_READY.value)
        if external and self.config.max_external_messages <= 0:
            reasons.append(DriverReason.BUDGET_EXHAUSTED.value)
        if external and not self.config.bounded_authorization_ready:
            reasons.append(DriverReason.BOUNDED_AUTHORIZATION_NOT_PROVED.value)
        target = manifest.safety.allowed_target_scope.get("actor_ref")
        if external and target and self.config.target_allowlist and target not in self.config.target_allowlist:
            reasons.append(DriverReason.TARGET_NOT_ALLOWED.value)
        return ScenarioPreflight(
            scenario_id=manifest.scenario.id,
            level=manifest.scenario.level,
            ready=not reasons,
            external_send_authorized=self.config.external_send_authorized,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def run_l0(
        self,
        session: Any,
        scenario_id: str,
        *,
        source_sha: str,
        runtime_sha: str | None = None,
        schema_revision: str | None = None,
        now: datetime | None = None,
    ) -> SyntheticL0Result:
        manifest = self.load(scenario_id)
        preflight = self.preflight(manifest)
        if manifest.scenario.level is not ScenarioTestLevel.L0_UNIT_DRY:
            return SyntheticL0Result(scenario_id, "BLOCKED", DriverReason.L1_NOT_AUTHORIZED.value)
        if not preflight.ready:
            return SyntheticL0Result(scenario_id, "NOT_READY", preflight.reasons[0])
        result: L0RunResult = execute_l0_manifest(
            session,
            manifest=manifest,
            manifest_path=str(self.manifest_root / f"{scenario_id}.yaml"),
            source_sha=source_sha,
            runtime_sha=runtime_sha or self.config.runtime_revision,
            schema_revision=schema_revision or self.config.schema_revision,
            driver_revision=self.config.runtime_revision,
            now=now,
        )
        return SyntheticL0Result(
            scenario_id=result.scenario_id,
            status=result.status,
            reason_code=result.reason_code,
            run_id=result.run_id,
            evidence_ref=result.evidence_ref,
        )

    def check_bounded_authorization(self, session: Any, **scope: Any) -> BoundedAuthorizationResult:
        """Evaluate the persisted authorization immediately before admission."""
        return check_bounded_authorization(session, **scope)

    def authorize_external_send(self, *, run_id: str, target_ref: str, budget: int) -> None:
        """Explicit future hook; never enabled by configuration alone."""
        if not self.config.external_send_authorized:
            raise PermissionError(DriverReason.EXTERNAL_SEND_NOT_AUTHORIZED.value)
        if budget != 1 or target_ref not in self.config.target_allowlist:
            raise PermissionError(DriverReason.TARGET_NOT_ALLOWED.value)
        raise PermissionError("EXTERNAL_SEND_REQUIRES_PLATFORM_EXECUTION_AUTHORIZATION")


def driver_status(config: SyntheticDriverConfig | None = None) -> dict[str, Any]:
    active = config or SyntheticDriverConfig()
    return {
        "service_ready": True,
        "scenario_engine_ready": True,
        "external_send_authorized": active.external_send_authorized,
        "max_allowed_level": active.max_allowed_level.value,
        "runtime_revision": active.runtime_revision,
        "schema_revision": active.schema_revision,
        "synthetic_actor_is_owner": False,
    }
