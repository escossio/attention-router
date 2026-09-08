from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AutonomyLevel(StrEnum):
    OBSERVE = "observe"
    SUGGEST = "suggest"
    APPROVAL_REQUIRED = "approval_required"
    LIMITED_AUTONOMY = "limited_autonomy"
    AUTONOMOUS = "autonomous"


class AutonomyExecutionMode(StrEnum):
    OBSERVE = "OBSERVE"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    AUTO_ALLOWED = "AUTO_ALLOWED"


class BlueprintStatus(StrEnum):
    DRAFT = "draft"
    READY_FOR_SIMULATION = "ready_for_simulation"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ConfigurationSessionStatus(StrEnum):
    ACTIVE = "active"
    READY_FOR_REVIEW = "ready_for_review"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class IdentitySpec(BaseModel):
    name: str = ""
    description: str = ""


class ToneSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = ""
    notes: str = ""


class ActionSpec(BaseModel):
    allowed: list[str] = Field(default_factory=list)
    requires_approval: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class AutonomySpec(BaseModel):
    level: AutonomyLevel = AutonomyLevel.OBSERVE
    execution_mode: AutonomyExecutionMode | None = None
    notes: str = "Blueprints start in observe mode until an explicit future autonomy change."

    @field_validator("level")
    @classmethod
    def reject_unsafe_initial_autonomy(cls, value: AutonomyLevel) -> AutonomyLevel:
        if value not in {AutonomyLevel.OBSERVE, AutonomyLevel.SUGGEST, AutonomyLevel.APPROVAL_REQUIRED}:
            raise ValueError("new blueprints cannot start with autonomous execution")
        return value


class AgentBlueprintSpec(BaseModel):
    schema_version: int = 1
    identity: IdentitySpec = Field(default_factory=IdentitySpec)
    purpose: str = ""
    domain: str = "general"
    audiences: list[str] = Field(default_factory=list)
    tone: ToneSpec = Field(default_factory=ToneSpec)
    goals: list[str] = Field(default_factory=list)
    knowledge_requirements: list[str] = Field(default_factory=list)
    known_information: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    actions: ActionSpec = Field(default_factory=ActionSpec)
    escalation: list[str] = Field(default_factory=list)
    autonomy: AutonomySpec = Field(default_factory=AutonomySpec)
    channels: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    configuration_completeness: int = Field(default=0, ge=0, le=100)
    blocking_questions: list[str] = Field(default_factory=list)
    behavior: dict[str, Any] = Field(default_factory=dict)


class Question(BaseModel):
    stage: str
    text: str


class InterviewResult(BaseModel):
    next_stage: str
    status: ConfigurationSessionStatus


class BlueprintPatchOperation(StrEnum):
    SET = "set"
    APPEND = "append"
    REMOVE = "remove"


BlueprintPatchValue = str | bool | float | list[str] | ToneSpec | None


class BlueprintPatch(BaseModel):
    path: str
    operation: BlueprintPatchOperation = BlueprintPatchOperation.SET
    value: BlueprintPatchValue = None
    source: str = "ai"


class RequirementEvidenceCoverage(StrEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"


class RequirementEvidence(BaseModel):
    requirement_id: str
    evidence_paths: list[str] = Field(default_factory=list)
    coverage: RequirementEvidenceCoverage = RequirementEvidenceCoverage.PARTIAL


class InterviewerTurn(BaseModel):
    assistant_message: str
    next_question: str
    proposed_updates: list[BlueprintPatch] = Field(default_factory=list)
    requirement_evidence: list[RequirementEvidence] = Field(default_factory=list)
    new_missing_information: list[str] = Field(default_factory=list)
    resolved_information: list[str] = Field(default_factory=list)
    ready_for_review: bool = False
    needs_clarification: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
