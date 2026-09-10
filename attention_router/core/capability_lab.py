from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from attention_router.core.authority import AuthorityResult
from attention_router.core.capabilities import CAPABILITY_NAME_RE


CAPABILITY_LAB_SCHEMA_VERSION = "capability-lab.v0"


class SimulatedHumanDecision(StrEnum):
    NONE = "NONE"
    APPROVE = "APPROVE"
    DENY = "DENY"


class ExpectedGrantMode(StrEnum):
    """Lab expectation only; it does not define the runtime grant storage contract."""

    NONE = "NONE"
    ONE_TIME = "ONE_TIME"
    TIME_BOUND = "TIME_BOUND"
    PERSISTENT = "PERSISTENT"


class ComparisonStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class CapabilityLabScenario(BaseModel):
    """Synthetic acceptance scenario for feature validation in the Capability Lab.

    This contract describes expected behavior around the existing capability and
    authority runtime. It cannot grant execution authority or produce a production
    external effect.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.v0"] = CAPABILITY_LAB_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=800)
    capability_key: str
    requester_actor_key: str = Field(pattern=r"^synthetic[.:/][a-zA-Z0-9_.:/-]{1,119}$")
    request_text: str = Field(min_length=1, max_length=1000)
    request_context: dict[str, Any] = Field(default_factory=dict)
    expected_resolution: AuthorityResult
    simulated_human_decision: SimulatedHumanDecision = SimulatedHumanDecision.NONE
    expected_grant_mode: ExpectedGrantMode = ExpectedGrantMode.NONE
    engine_scenario_key: str | None = Field(default=None, pattern=r"^SCN-PE-[0-9]{3}$")
    tags: list[str] = Field(default_factory=list)
    synthetic_only: Literal[True] = True
    uses_real_personal_data: Literal[False] = False
    production_effects_allowed: Literal[False] = False

    @field_validator("capability_key")
    @classmethod
    def canonical_capability_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CAPABILITY_NAME_RE.fullmatch(normalized):
            raise ValueError("capability_key must use the canonical dotted capability name")
        return normalized

    @model_validator(mode="after")
    def validate_expected_flow(self) -> "CapabilityLabScenario":
        if self.expected_resolution != AuthorityResult.REQUIRES_APPROVAL:
            if self.simulated_human_decision != SimulatedHumanDecision.NONE:
                raise ValueError("human decision is only valid for REQUIRES_APPROVAL scenarios")
            if self.expected_grant_mode != ExpectedGrantMode.NONE:
                raise ValueError("non-approval scenarios cannot expect a permission grant")
            return self

        if self.simulated_human_decision != SimulatedHumanDecision.APPROVE:
            if self.expected_grant_mode != ExpectedGrantMode.NONE:
                raise ValueError("only simulated approval can expect a permission grant")
        return self


class CapabilityLabObservation(BaseModel):
    """Observed staged result supplied to the lab comparator; never an execution command."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.v0"] = CAPABILITY_LAB_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    observed_resolution: AuthorityResult | None = None
    observed_reason_code: str | None = Field(default=None, max_length=160)
    observed_human_decision: SimulatedHumanDecision | None = None
    observed_grant_mode: ExpectedGrantMode | None = None
    matched_rule_id: str | None = Field(default=None, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    production_effect_observed: bool = False

    @model_validator(mode="after")
    def validate_observation(self) -> "CapabilityLabObservation":
        if self.observed_resolution is None:
            if self.observed_human_decision is not None or self.observed_grant_mode is not None:
                raise ValueError("later-stage observations require an observed resolution")
        return self


class CapabilityLabComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    status: ComparisonStatus
    mismatches: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def _grant_mismatch(
    expected: ExpectedGrantMode,
    observed: ExpectedGrantMode,
) -> str:
    return f"GRANT_MODE_MISMATCH:expected={expected};observed={observed}"


def compare_scenario(
    scenario: CapabilityLabScenario,
    observation: CapabilityLabObservation,
) -> CapabilityLabComparison:
    """Compare T0 authority, T1 human decision and T2 grant without mutating runtime."""

    mismatches: list[str] = []
    incomplete: list[str] = []

    if scenario.scenario_id != observation.scenario_id:
        mismatches.append("SCENARIO_ID_MISMATCH")

    if observation.production_effect_observed:
        mismatches.append("PRODUCTION_EFFECT_OBSERVED")

    if observation.errors:
        mismatches.extend(f"OBSERVATION_ERROR:{error}" for error in observation.errors)

    if observation.observed_resolution is None:
        incomplete.append("T0_RESOLUTION_NOT_OBSERVED")
    elif observation.observed_resolution != scenario.expected_resolution:
        mismatches.append(
            "RESOLUTION_MISMATCH:"
            f"expected={scenario.expected_resolution};observed={observation.observed_resolution}"
        )
    elif scenario.expected_resolution == AuthorityResult.REQUIRES_APPROVAL:
        expected_decision = scenario.simulated_human_decision
        observed_decision = observation.observed_human_decision

        if expected_decision == SimulatedHumanDecision.NONE:
            if observed_decision not in {None, SimulatedHumanDecision.NONE}:
                mismatches.append(
                    "UNEXPECTED_HUMAN_DECISION:"
                    f"observed={observed_decision}"
                )
            if observation.observed_grant_mode not in {None, ExpectedGrantMode.NONE}:
                mismatches.append(
                    _grant_mismatch(ExpectedGrantMode.NONE, observation.observed_grant_mode)
                )
        elif observed_decision is None:
            incomplete.append("T1_HUMAN_DECISION_NOT_OBSERVED")
        elif observed_decision != expected_decision:
            mismatches.append(
                "HUMAN_DECISION_MISMATCH:"
                f"expected={expected_decision};observed={observed_decision}"
            )
        elif expected_decision == SimulatedHumanDecision.APPROVE:
            if scenario.expected_grant_mode == ExpectedGrantMode.NONE:
                if observation.observed_grant_mode not in {None, ExpectedGrantMode.NONE}:
                    mismatches.append(
                        _grant_mismatch(
                            ExpectedGrantMode.NONE,
                            observation.observed_grant_mode,
                        )
                    )
            elif observation.observed_grant_mode is None:
                incomplete.append("T2_GRANT_NOT_OBSERVED")
            elif observation.observed_grant_mode != scenario.expected_grant_mode:
                mismatches.append(
                    _grant_mismatch(
                        scenario.expected_grant_mode,
                        observation.observed_grant_mode,
                    )
                )
        elif observation.observed_grant_mode not in {None, ExpectedGrantMode.NONE}:
            mismatches.append(
                _grant_mismatch(ExpectedGrantMode.NONE, observation.observed_grant_mode)
            )
    else:
        if observation.observed_human_decision not in {None, SimulatedHumanDecision.NONE}:
            mismatches.append(
                "UNEXPECTED_HUMAN_DECISION:"
                f"observed={observation.observed_human_decision}"
            )
        if observation.observed_grant_mode not in {None, ExpectedGrantMode.NONE}:
            mismatches.append(
                _grant_mismatch(ExpectedGrantMode.NONE, observation.observed_grant_mode)
            )

    if mismatches:
        status = ComparisonStatus.FAIL
    elif incomplete:
        status = ComparisonStatus.INCOMPLETE
    else:
        status = ComparisonStatus.PASS

    return CapabilityLabComparison(
        scenario_id=scenario.scenario_id,
        status=status,
        mismatches=mismatches,
        incomplete_reasons=incomplete,
        evidence_refs=observation.evidence_refs,
    )
