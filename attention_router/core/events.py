from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id


class EventOrigin(StrEnum):
    EXTERNAL_INBOUND = "EXTERNAL_INBOUND"
    OWNER_COMMAND = "OWNER_COMMAND"
    SYSTEM_EVENT = "SYSTEM_EVENT"
    PROVIDER_EVENT = "PROVIDER_EVENT"
    DEVICE_EVENT = "DEVICE_EVENT"
    SCHEDULED_EVENT = "SCHEDULED_EVENT"
    INTERNAL_EVENT = "INTERNAL_EVENT"


class OwnerCommandUnauthorized(PermissionError):
    pass


class OperatorAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = DEFAULT_TENANT_ID
    operator_actor_id: str
    authenticated: bool = False
    roles: list[str] = Field(default_factory=list)

    @property
    def can_issue_owner_commands(self) -> bool:
        return self.authenticated and bool({"OWNER", "OPERATOR"} & {role.upper() for role in self.roles})


class EventEnvelope(BaseModel):
    """Canonical event contract; payloads remain referenced, not copied by default."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=new_id)
    tenant_id: str = DEFAULT_TENANT_ID
    origin: EventOrigin
    event_type: str = Field(min_length=1, max_length=120)
    actor_id: str | None = Field(default=None, max_length=120)
    resource_id: str | None = Field(default=None, max_length=64)
    channel: str | None = Field(default=None, max_length=80)
    payload_type: str = Field(min_length=1, max_length=80)
    payload_ref: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    received_at: datetime
    correlation_id: str = Field(min_length=1, max_length=64)
    causation_id: str | None = Field(default=None, max_length=64)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def authorize_origin(
    requested: EventOrigin,
    authority: OperatorAuthority | None = None,
) -> EventOrigin:
    """Authority comes from authenticated ingress context, never from message text."""
    if requested != EventOrigin.OWNER_COMMAND:
        return requested
    if authority is None or not authority.can_issue_owner_commands:
        raise OwnerCommandUnauthorized("OWNER_COMMAND_REQUIRES_AUTHENTICATED_OPERATOR")
    return EventOrigin.OWNER_COMMAND
