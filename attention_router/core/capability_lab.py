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
