from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping

from attention_router.platform.scenarios import (
    EffectClass,
    ScenarioManifest,
    ScenarioTestLevel,
)


class MatrixContractError(ValueError):
    pass


class MatrixItemStatus(StrEnum):
    NOT_EXECUTED = "NOT_EXECUTED"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class MatrixStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_EXECUTED = "NOT_EXECUTED"


@dataclass(frozen=True, slots=True)
class ExternalExecutionAuthorization:
    scenario_id: str
    scenario_version: int
    scenario_content_hash: str
    tenant_id: str
    scenario_run_id: str
    expires_at: datetime
    target_scope: Mapping[str, Any]
    driver_revision: str
    driver_session_id: str
    owner_session_id: str
    requested_by_human: bool
    gate_e1_passed: bool
    lease_reserved: bool
    budget_reserved: bool
    readiness_fresh: bool
    provenance_matched: bool
    driver_ready: bool
    driver_session_isolated: bool
    driver_disabled_by_default_proved: bool
    evidence_refs: tuple[str, ...]

    def valid_at(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return all(
            (
                bool(self.tenant_id),
                bool(self.scenario_run_id),
                bool(self.scenario_content_hash),
                bool(self.driver_revision),
                bool(self.driver_session_id),
                bool(self.owner_session_id),
                self.driver_session_id != self.owner_session_id,
                bool(self.evidence_refs),
                timestamp < expires_at,
                self.requested_by_human,
                self.gate_e1_passed,
                self.lease_reserved,
                self.budget_reserved,
                self.readiness_fresh,
                self.provenance_matched,
                self.driver_ready,
                self.driver_session_isolated,
                self.driver_disabled_by_default_proved,
            )
        )


@dataclass(frozen=True, slots=True)
class PlannedScenario:
    scenario_id: str
    version: int
    content_hash: str
    level: ScenarioTestLevel
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class ScenarioExecutionResult:
    scenario_id: str
    status: MatrixItemStatus
    reason_code: str
    acceptance_ids: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    risk_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatrixSummary:
    status: MatrixStatus
    scenario_count: int
    pass_count: int
    fail_count: int
    blocked_count: int
    not_executed_count: int
    acceptance_ids: frozenset[str]
    invariant_ids: frozenset[str]
    risk_ids: frozenset[str]


class AutomatedScenarioMatrix:
    """Build and aggregate plans without granting authority or changing gates."""

    def __init__(self, manifests: Iterable[ScenarioManifest]) -> None:
        materialized = tuple(manifests)
        self._manifests = {manifest.scenario.id: manifest for manifest in materialized}
        if len(self._manifests) != len(materialized):
            raise MatrixContractError("DUPLICATE_SCENARIO_ID")

    def plan(
        self,
        scenario_ids: Iterable[str],
        *,
        external_authorizations: Mapping[str, ExternalExecutionAuthorization] | None = None,
        tenant_id: str | None = None,
        scenario_run_ids: Mapping[str, str] | None = None,
        driver_revision: str | None = None,
        now: datetime | None = None,
    ) -> tuple[PlannedScenario, ...]:
        authorizations = external_authorizations or {}
        expected_runs = scenario_run_ids or {}
        timestamp = now or datetime.now(UTC)
        planned: list[PlannedScenario] = []
        for scenario_id in scenario_ids:
            try:
                manifest = self._manifests[scenario_id]
            except KeyError as exc:
                raise MatrixContractError(f"UNKNOWN_SCENARIO:{scenario_id}") from exc
            blocked_reason = self._external_blocker(
                manifest,
                authorizations.get(scenario_id),
                expected_tenant_id=tenant_id,
                expected_scenario_run_id=expected_runs.get(scenario_id),
                expected_driver_revision=driver_revision,
                now=timestamp,
            )
            planned.append(
                PlannedScenario(
                    scenario_id=scenario_id,
                    version=manifest.scenario.version,
                    content_hash=manifest.content_hash(),
                    level=manifest.scenario.level,
                    blocked_reason=blocked_reason,
                )
            )
        return tuple(planned)

    @staticmethod
    def _external_blocker(
        manifest: ScenarioManifest,
        authorization: ExternalExecutionAuthorization | None,
        *,
        expected_tenant_id: str | None,
        expected_scenario_run_id: str | None,
        expected_driver_revision: str | None,
        now: datetime,
    ) -> str | None:
        external = manifest.scenario.effect_class in {
            EffectClass.SYNTHETIC_EXTERNAL_EFFECT,
            EffectClass.HUMAN_CANARY_EXTERNAL_EFFECT,
        }
        if not external:
            return None
        if authorization is None:
            return "EXTERNAL_EFFECT_NOT_AUTHORIZED"
        if not expected_tenant_id or not expected_scenario_run_id or not expected_driver_revision:
            return "EXTERNAL_EXECUTION_CONTEXT_MISSING"
        if authorization.scenario_id != manifest.scenario.id:
            return "EXTERNAL_AUTHORIZATION_SCENARIO_MISMATCH"
        if authorization.tenant_id != expected_tenant_id:
            return "EXTERNAL_AUTHORIZATION_TENANT_MISMATCH"
        if authorization.scenario_run_id != expected_scenario_run_id:
            return "EXTERNAL_AUTHORIZATION_RUN_MISMATCH"
        if authorization.driver_revision != expected_driver_revision:
            return "EXTERNAL_AUTHORIZATION_DRIVER_REVISION_MISMATCH"
        if authorization.scenario_version != manifest.scenario.version:
            return "EXTERNAL_AUTHORIZATION_VERSION_MISMATCH"
        if authorization.scenario_content_hash != manifest.content_hash():
            return "EXTERNAL_AUTHORIZATION_HASH_MISMATCH"
        if dict(authorization.target_scope) != dict(manifest.safety.allowed_target_scope):
            return "EXTERNAL_AUTHORIZATION_TARGET_SCOPE_MISMATCH"
        if not authorization.valid_at(now):
            return "EXTERNAL_EFFECT_PREREQUISITES_NOT_SATISFIED"
        return None

    @staticmethod
    def summarize(results: Iterable[ScenarioExecutionResult]) -> MatrixSummary:
        materialized = tuple(results)
        statuses = [result.status for result in materialized]
        if MatrixItemStatus.FAIL in statuses:
            status = MatrixStatus.FAIL
        elif MatrixItemStatus.BLOCKED in statuses:
            status = MatrixStatus.BLOCKED
        elif not materialized or MatrixItemStatus.NOT_EXECUTED in statuses:
            status = MatrixStatus.NOT_EXECUTED
        else:
            status = MatrixStatus.PASS
        return MatrixSummary(
            status=status,
            scenario_count=len(materialized),
            pass_count=statuses.count(MatrixItemStatus.PASS),
            fail_count=statuses.count(MatrixItemStatus.FAIL),
            blocked_count=statuses.count(MatrixItemStatus.BLOCKED),
            not_executed_count=statuses.count(MatrixItemStatus.NOT_EXECUTED),
            acceptance_ids=frozenset(
                item for result in materialized for item in result.acceptance_ids
            ),
            invariant_ids=frozenset(
                item for result in materialized for item in result.invariant_ids
            ),
            risk_ids=frozenset(item for result in materialized for item in result.risk_ids),
        )
