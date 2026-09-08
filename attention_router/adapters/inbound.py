from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.core.tenancy import DEFAULT_TENANT_ID


class NormalizedInboundEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    tenant_id: str = DEFAULT_TENANT_ID
    source: str = Field(min_length=1, max_length=120)
    external_event_id: str = Field(min_length=1, max_length=180)
    event_type: Literal["message", "call"]
    occurred_at: datetime | None = None
    received_at: datetime = Field(default_factory=now_utc)
    actor_id: str = Field(min_length=1, max_length=120)
    actor_display_name: str = Field(min_length=1, max_length=160)
    actor_category: str = Field(min_length=1, max_length=120)
    active_context: str | None = Field(default=None, max_length=120)
    channel: str = Field(default="synthetic", max_length=80)
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_origin: str | None = Field(default=None, max_length=80)
    owner_authenticated: bool = False
    lineage_classification: Literal["ORGANIC", "SYNTHETIC", "HISTORICAL_UNKNOWN"] = "ORGANIC"
    scenario_id: str | None = Field(default=None, max_length=64)
    scenario_run_id: str | None = Field(default=None, max_length=64)
    scenario_step_run_id: str | None = Field(default=None, max_length=64)
    stimulus_id: str | None = Field(default=None, max_length=128)
    correlation_id: str = Field(default_factory=new_id)
    raw_reference: str | None = None

    @field_validator("occurred_at", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def validate_structural_lineage(self) -> "NormalizedInboundEvent":
        message_type = str(self.metadata.get("message_type") or "").casefold()
        owner_observation = self.event_origin in {
            "OWNER_MANUAL_OUTBOUND_OBSERVED",
            "ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
            "UNKNOWN_FROM_ME",
        }
        if not self.content.strip() and message_type not in {"ptt", "audio"} and not owner_observation:
            raise ValueError("INBOUND_CONTENT_REQUIRED")
        scenario_values = (
            self.scenario_id,
            self.scenario_run_id,
            self.scenario_step_run_id,
            self.stimulus_id,
        )
        if self.lineage_classification == "SYNTHETIC":
            if not self.scenario_id or not self.scenario_run_id or not self.stimulus_id:
                raise ValueError("SYNTHETIC_SCENARIO_LINEAGE_REQUIRED")
        elif any(scenario_values):
            raise ValueError("NON_SYNTHETIC_SCENARIO_LINEAGE_FORBIDDEN")
        return self

    @property
    def normalized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"correlation_id", "received_at", "tenant_id"})

    @property
    def payload_hash(self) -> str:
        return stable_hash(self.normalized_payload)


class InboundAdapter(Protocol):
    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent: ...

    def normalize_many(self, raw_event: dict[str, Any]) -> list[NormalizedInboundEvent]:
        return [self.normalize(raw_event)]
