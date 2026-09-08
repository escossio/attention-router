from typing import Any, Literal

from pydantic import BaseModel, Field

from attention_router.core.capabilities import CapabilityRequest


class ActionRequest(BaseModel):
    action_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    user_requested: bool = False


class AndyAgentOutput(BaseModel):
    response_text: str = ""
    intent: str
    objective: str
    reason_code: str
    confidence: Literal["high", "medium", "low"]
    needs_more_information: bool = False
    missing_information: list[str] = Field(default_factory=list)
    requested_actions: list[ActionRequest] = Field(default_factory=list)
    requested_capabilities: list[CapabilityRequest] = Field(default_factory=list)
    conversation_state: Literal["answer", "clarify", "action_requested", "hold"]
    safety_flags: list[str] = Field(default_factory=list)
