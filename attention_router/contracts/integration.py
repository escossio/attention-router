"""Language-neutral integration contracts for channels and capability providers.

The contract is deliberately provider-agnostic. Provider-specific adapters translate
their native payloads into these models. Tenant identity is always explicit and must
come from a trusted binding/authentication boundary, never from untrusted message text.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


CANONICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IntegrationKind(StrEnum):
    CHANNEL = "CHANNEL"
    CAPABILITY = "CAPABILITY"


class IntegrationThreadKind(StrEnum):
    DIRECT = "DIRECT"
    GROUP = "GROUP"
    THREAD = "THREAD"
    CHANNEL = "CHANNEL"


class IntegrationResultStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    ACCEPTED = "ACCEPTED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class IntegrationErrorClass(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class IntegrationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntegrationKind
    name: str = Field(min_length=3, max_length=120)
    instance_id: str = Field(min_length=1, max_length=120)
    account_id: str | None = Field(default=None, max_length=180)

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CANONICAL_NAME_RE.fullmatch(normalized):
            raise ValueError("integration name must use a dotted canonical name")
        return normalized

    @field_validator("instance_id", "account_id")
    @classmethod
    def strip_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("integration identifier cannot be blank")
        return normalized


class ExternalActorRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_actor_id: str = Field(min_length=1, max_length=240)
    display_name: str | None = Field(default=None, max_length=160)


class ExternalThreadRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_thread_id: str = Field(min_length=1, max_length=240)
    kind: IntegrationThreadKind = IntegrationThreadKind.THREAD
    title: str | None = Field(default=None, max_length=240)


class ArtifactReceiptContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["artifact_receipt"] = "artifact_receipt"
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1, max_length=64)
    source: IntegrationSource
    external_receipt_id: str = Field(min_length=1, max_length=240)
    content_sha256: str = Field(min_length=64, max_length=64)
    artifact_kind: str = Field(min_length=1, max_length=40)
    mime_type: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(ge=0)
    storage_provider: str = Field(min_length=1, max_length=80)
    storage_reference: str = Field(min_length=1, max_length=512)
    received_at: datetime
    sender: ExternalActorRef | None = None
    original_filename: str | None = Field(default=None, max_length=512)
    artifact_metadata: dict[str, Any] = Field(default_factory=dict)
    receipt_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not SHA256_RE.fullmatch(normalized):
            raise ValueError("content_sha256 must be lowercase hex sha256")
        return normalized

    @field_validator("artifact_kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("mime_type", "storage_provider")
    @classmethod
    def normalize_lower(cls, value: str) -> str:
        return value.strip().casefold()

    @field_validator("received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class InboundIntegrationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["inbound_event"] = "inbound_event"
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1, max_length=64)
    source: IntegrationSource
    external_event_id: str = Field(min_length=1, max_length=240)
    event_type: str = Field(min_length=1, max_length=120)
    actor: ExternalActorRef | None = None
    thread: ExternalThreadRef | None = None
    payload_type: str = Field(min_length=1, max_length=80)
    payload_ref: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    occurred_at: datetime
    received_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=240)
    correlation_id: str = Field(min_length=1, max_length=64)
    causation_id: str | None = Field(default=None, max_length=64)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifact_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("artifact_ids cannot contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("artifact_ids must be unique")
        return normalized


class ChannelDeliveryContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["channel_delivery"] = "channel_delivery"
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1, max_length=64)
    integration: IntegrationSource
    target_binding_id: str = Field(min_length=1, max_length=120)
    payload_type: str = Field(min_length=1, max_length=80)
    payload_ref: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=240)
    correlation_id: str = Field(min_length=1, max_length=64)
    causation_id: str | None = Field(default=None, max_length=64)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifact_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("artifact_ids cannot contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("artifact_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def require_channel(self) -> "ChannelDeliveryContract":
        if self.integration.kind != IntegrationKind.CHANNEL:
            raise ValueError("channel delivery requires CHANNEL integration")
        return self


class CapabilityInvocationContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["capability_invocation"] = "capability_invocation"
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1, max_length=64)
    integration: IntegrationSource
    capability: str = Field(min_length=3, max_length=160)
    target: dict[str, Any] | None = None
    resource: dict[str, Any] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    user_requested: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"
    idempotency_key: str = Field(min_length=1, max_length=240)
    correlation_id: str = Field(min_length=1, max_length=64)
    causation_id: str | None = Field(default=None, max_length=64)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capability")
    @classmethod
    def canonical_capability(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CANONICAL_NAME_RE.fullmatch(normalized):
            raise ValueError("capability must use a dotted canonical name")
        return normalized

    @model_validator(mode="after")
    def require_capability_integration(self) -> "CapabilityInvocationContract":
        if self.integration.kind != IntegrationKind.CAPABILITY:
            raise ValueError("capability invocation requires CAPABILITY integration")
        return self


class IntegrationResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_type: Literal["integration_result"] = "integration_result"
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1, max_length=64)
    integration: IntegrationSource
    request_id: str = Field(min_length=1, max_length=240)
    correlation_id: str = Field(min_length=1, max_length=64)
    status: IntegrationResultStatus
    reason_code: str = Field(min_length=1, max_length=120)
    provider_reference: str | None = Field(default=None, max_length=240)
    error_class: IntegrationErrorClass | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400)
    details_sanitized: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason_code")
    @classmethod
    def valid_reason_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not REASON_CODE_RE.fullmatch(normalized):
            raise ValueError("reason_code must be uppercase snake case")
        return normalized

    @model_validator(mode="after")
    def coherent_result(self) -> "IntegrationResultContract":
        failed = self.status in {
            IntegrationResultStatus.RETRYABLE_FAILURE,
            IntegrationResultStatus.PERMANENT_FAILURE,
        }
        if failed and self.error_class is None:
            raise ValueError("failure result requires error_class")
        if not failed and self.error_class is not None:
            raise ValueError("success result cannot carry error_class")
        if self.retry_after_seconds is not None and self.status != IntegrationResultStatus.RETRYABLE_FAILURE:
            raise ValueError("retry_after_seconds requires RETRYABLE_FAILURE")
        return self


IntegrationContractMessage = Annotated[
    ArtifactReceiptContract
    | InboundIntegrationEvent
    | ChannelDeliveryContract
    | CapabilityInvocationContract
    | IntegrationResultContract,
    Field(discriminator="contract_type"),
]


def integration_contract_json_schema() -> dict[str, Any]:
    """Return the versioned language-neutral schema used by SDK implementations."""
    schema = TypeAdapter(IntegrationContractMessage).json_schema(
        ref_template="#/$defs/{model}"
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:attention-router:integration-contract:v1",
        **schema,
    }


__all__ = [
    "ArtifactReceiptContract",
    "CapabilityInvocationContract",
    "ChannelDeliveryContract",
    "ExternalActorRef",
    "ExternalThreadRef",
    "InboundIntegrationEvent",
    "IntegrationContractMessage",
    "IntegrationErrorClass",
    "IntegrationKind",
    "IntegrationResultContract",
    "IntegrationResultStatus",
    "IntegrationSource",
    "IntegrationThreadKind",
    "integration_contract_json_schema",
]
