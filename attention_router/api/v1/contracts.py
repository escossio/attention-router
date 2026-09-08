from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeviceBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_key: str | None = Field(default=None, max_length=120)
    resource_id: str | None = Field(default=None, max_length=64)


class MatrixCapabilityView(BaseModel):
    capability: str
    version: int | None
    state: str
    required_provider: str | None
    bound_provider: str | None
    health: str | None
    side_effect: bool | None
    sensitivity: str | None
