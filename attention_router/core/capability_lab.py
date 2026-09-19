from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from attention_router.core.authority import AuthorityResult
from attention_router.core.capabilities import CAPABILITY_NAME_RE, CapabilityResolutionStatus


CAPABILITY_LAB_T0_SCHEMA_VERSION = "capability-lab.t0.v1"
CAPABILITY_LAB_T1_SCHEMA_VERSION = "capability-lab.t1.v1"


class T0ComparisonStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class CapabilityLabT0Scenario(BaseModel):
    """Repository-owned synthetic expectation for canonical T0 authority resolution."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.t0.v1"] = CAPABILITY_LAB_T0_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=160)
    capability_key: str
    requester_actor_key: str = Field(pattern=r"^synthetic[.:/][a-zA-Z0-9_.:/-]{1,119}$")
    expected_resolution_status: CapabilityResolutionStatus
    expected_authority_result: AuthorityResult
    expected_reason_code: str = Field(min_length=1, max_length=160)
    synthetic_only: Literal[True] = True
    production_effects_allowed: Literal[False] = False

    @field_validator("capability_key")
    @classmethod
    def canonical_capability_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CAPABILITY_NAME_RE.fullmatch(normalized):
            raise ValueError("capability_key must use canonical dotted form")
        return normalized


class CapabilityLabT0Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    resolution_status: CapabilityResolutionStatus
    authority_result: AuthorityResult
    reason_code: str
    provider_interface: str | None = None
    approval_required: bool
    execution_allowed: bool
    evidence_refs: list[str] = Field(default_factory=list)
    production_effect_observed: Literal[False] = False


class CapabilityLabT0Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    status: T0ComparisonStatus
    mismatches: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def compare_t0(
    scenario: CapabilityLabT0Scenario,
    observation: CapabilityLabT0Observation,
    *,
    require_evidence: bool,
) -> CapabilityLabT0Comparison:
    mismatches: list[str] = []
    incomplete: list[str] = []

    if scenario.scenario_id != observation.scenario_id:
        mismatches.append("SCENARIO_ID_MISMATCH")
    if observation.resolution_status != scenario.expected_resolution_status:
        mismatches.append("RESOLUTION_STATUS_MISMATCH")
    if observation.authority_result != scenario.expected_authority_result:
        mismatches.append("AUTHORITY_RESULT_MISMATCH")
    if observation.reason_code != scenario.expected_reason_code:
        mismatches.append("REASON_CODE_MISMATCH")
    if require_evidence and not observation.evidence_refs:
        incomplete.append("T0_EVIDENCE_MISSING")

    status = (
        T0ComparisonStatus.FAIL
        if mismatches
        else T0ComparisonStatus.INCOMPLETE
        if incomplete
        else T0ComparisonStatus.PASS
    )
    return CapabilityLabT0Comparison(
        scenario_id=scenario.scenario_id,
        status=status,
        mismatches=mismatches,
        incomplete_reasons=incomplete,
        evidence_refs=list(observation.evidence_refs),
    )


class T1ComparisonStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class CapabilityLabT1Scenario(BaseModel):
    """Synthetic request boundary for canonical human execution authorization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.t1.v1"] = CAPABILITY_LAB_T1_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=160)
    capability_key: str
    t0_scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    requester_actor_key: str = Field(pattern=r"^synthetic[.:/][a-zA-Z0-9_.:/-]{1,119}$")
    expected_approver: str = Field(pattern=r"^synthetic[.:/][a-zA-Z0-9_.:/-]{1,119}$")
    ttl_seconds: int = Field(ge=30, le=900)
    expected_authorization_state: Literal["PENDING_HUMAN_APPROVAL"] = "PENDING_HUMAN_APPROVAL"
    synthetic_only: Literal[True] = True
    production_effects_allowed: Literal[False] = False

    @field_validator("capability_key")
    @classmethod
    def canonical_t1_capability_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CAPABILITY_NAME_RE.fullmatch(normalized):
            raise ValueError("capability_key must use canonical dotted form")
        return normalized


class CapabilityLabT1Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    authorization_state: str
    approval_channel: str
    fingerprint_matches: bool
    tenant_scope_matches: bool
    evidence_refs: list[str] = Field(default_factory=list)
    grant_created: Literal[False] = False
    production_effect_observed: Literal[False] = False


class CapabilityLabT1Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    status: T1ComparisonStatus
    mismatches: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def compare_t1(
    scenario: CapabilityLabT1Scenario,
    observation: CapabilityLabT1Observation,
    *,
    require_evidence: bool,
) -> CapabilityLabT1Comparison:
    mismatches: list[str] = []
    incomplete: list[str] = []

    if scenario.scenario_id != observation.scenario_id:
        mismatches.append("SCENARIO_ID_MISMATCH")
    if observation.authorization_state != scenario.expected_authorization_state:
        mismatches.append("AUTHORIZATION_STATE_MISMATCH")
    if observation.approval_channel != "meta_whatsapp_interactive":
        mismatches.append("APPROVAL_CHANNEL_MISMATCH")
    if not observation.fingerprint_matches:
        mismatches.append("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if not observation.tenant_scope_matches:
        mismatches.append("TENANT_SCOPE_MISMATCH")
    if observation.grant_created:
        mismatches.append("UNEXPECTED_GRANT_CREATED")
    if observation.production_effect_observed:
        mismatches.append("PRODUCTION_EFFECT_OBSERVED")
    if require_evidence and not observation.evidence_refs:
        incomplete.append("T1_EVIDENCE_MISSING")

    status = (
        T1ComparisonStatus.FAIL
        if mismatches
        else T1ComparisonStatus.INCOMPLETE
        if incomplete
        else T1ComparisonStatus.PASS
    )
    return CapabilityLabT1Comparison(
        scenario_id=scenario.scenario_id,
        status=status,
        mismatches=mismatches,
        incomplete_reasons=incomplete,
        evidence_refs=list(observation.evidence_refs),
    )
