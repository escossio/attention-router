from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from attention_router.core.control_plane import DecisionMode, GrantMode


CAPABILITY_LAB_SCHEMA_VERSION = "capability-lab.v0"


class SimulatedHumanDecision(StrEnum):
    NONE = "NONE"
    APPROVE = "APPROVE"
    DENY = "DENY"


class ComparisonStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class CapabilityLabScenario(BaseModel):
    """Synthetic acceptance scenario for feature validation in the Capability Lab.

    This contract describes an expected behavior. It is deliberately incapable of
    representing permission to produce a production external effect.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.v0"] = CAPABILITY_LAB_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=800)
    capability_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    requester_actor_key: str = Field(pattern=r"^synthetic[.:/][a-zA-Z0-9_.:/-]{1,119}$")
    request_text: str = Field(min_length=1, max_length=1000)
    request_context: dict[str, Any] = Field(default_factory=dict)
    expected_resolution: DecisionMode
    simulated_human_decision: SimulatedHumanDecision = SimulatedHumanDecision.NONE
    expected_grant_mode: GrantMode = GrantMode.NONE
    tags: list[str] = Field(default_factory=list)
    synthetic_only: Literal[True] = True
    uses_real_personal_data: Literal[False] = False
    production_effects_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_expected_flow(self) -> "CapabilityLabScenario":
        if self.expected_resolution != DecisionMode.REQUIRE_APPROVAL:
            if self.simulated_human_decision != SimulatedHumanDecision.NONE:
                raise ValueError("human decision is only valid for REQUIRE_APPROVAL scenarios")
            if self.expected_grant_mode != GrantMode.NONE:
                raise ValueError("non-approval scenarios cannot expect a permission grant")
            return self

        if self.simulated_human_decision != SimulatedHumanDecision.APPROVE:
            if self.expected_grant_mode != GrantMode.NONE:
                raise ValueError("only simulated approval can expect a permission grant")
        return self


class CapabilityLabObservation(BaseModel):
    """Observed result supplied to the lab comparator; never an execution command."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["capability-lab.v0"] = CAPABILITY_LAB_SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,119}$")
    observed_resolution: DecisionMode | None = None
    observed_grant_mode: GrantMode = GrantMode.NONE
    matched_rule_id: str | None = Field(default=None, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    production_effect_observed: bool = False

    @model_validator(mode="after")
    def validate_observation(self) -> "CapabilityLabObservation":
        if self.observed_resolution is None and self.observed_grant_mode != GrantMode.NONE:
            raise ValueError("a grant observation requires an observed resolution")
        return self


class CapabilityLabComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    status: ComparisonStatus
    mismatches: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def compare_scenario(
    scenario: CapabilityLabScenario,
    observation: CapabilityLabObservation,
) -> CapabilityLabComparison:
    """Compare expected and observed behavior without mutating runtime state."""

    mismatches: list[str] = []

    if scenario.scenario_id != observation.scenario_id:
        mismatches.append("SCENARIO_ID_MISMATCH")

    if observation.production_effect_observed:
        mismatches.append("PRODUCTION_EFFECT_OBSERVED")

    if observation.errors:
        mismatches.extend(f"OBSERVATION_ERROR:{error}" for error in observation.errors)

    if observation.observed_resolution is None:
        status = ComparisonStatus.FAIL if mismatches else ComparisonStatus.INCOMPLETE
        return CapabilityLabComparison(
            scenario_id=scenario.scenario_id,
            status=status,
            mismatches=mismatches,
            evidence_refs=observation.evidence_refs,
        )

    if observation.observed_resolution != scenario.expected_resolution:
        mismatches.append(
            "RESOLUTION_MISMATCH:"
            f"expected={scenario.expected_resolution};observed={observation.observed_resolution}"
        )

    if observation.observed_grant_mode != scenario.expected_grant_mode:
        mismatches.append(
            "GRANT_MODE_MISMATCH:"
            f"expected={scenario.expected_grant_mode};observed={observation.observed_grant_mode}"
        )

    return CapabilityLabComparison(
        scenario_id=scenario.scenario_id,
        status=ComparisonStatus.FAIL if mismatches else ComparisonStatus.PASS,
        mismatches=mismatches,
        evidence_refs=observation.evidence_refs,
    )
