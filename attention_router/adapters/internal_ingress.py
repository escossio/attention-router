from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from attention_router.adapters.inbound import InboundAdapter, NormalizedInboundEvent
from attention_router.config import settings


class InternalInboundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    source: Literal["wwebjs"]
    external_event_id: str = Field(min_length=1, max_length=180)
    event_type: Literal["message", "call"]
    occurred_at: datetime | None = None
    received_at: datetime | None = None
    external_actor_id: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="whatsapp", max_length=80)
    message_type: str = Field(default="text", max_length=80)
    content: str = Field(default="", max_length=8000)
    has_media: bool = False
    event_origin: str | None = Field(default=None, max_length=80)
    owner_authenticated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_message_contract(self):
        observations = {
            "OWNER_MANUAL_OUTBOUND_OBSERVED",
            "ROUTER_AUTOMATED_OUTBOUND_OBSERVED",
            "UNKNOWN_FROM_ME",
        }
        if self.event_origin in observations:
            if self.owner_authenticated or self.metadata.get("from_me") is not True:
                raise ValueError("owner outbound observation must be from_me and not an Owner Command")
            if self.metadata.get("from_me_classification") != self.event_origin:
                raise ValueError("owner outbound observation classification mismatch")
            return self
        if self.event_origin == "OWNER_COMMAND":
            final_classification = self.metadata.get("final_from_me_classification")
            from_me_classification = self.metadata.get("from_me_classification")
            if (
                self.owner_authenticated is not True
                or self.metadata.get("from_me") is not True
                or self.metadata.get("owner_self_chat") is not True
                or final_classification != "OWNER_COMMAND"
                or from_me_classification != "OWNER_COMMAND"
            ):
                raise ValueError("Owner Command authentication structure is invalid")
        elif self.owner_authenticated:
            raise ValueError("owner_authenticated requires Owner Command origin")
        if not self.has_media and self.message_type == "text" and not self.content.strip():
            raise ValueError("text message content is required")
        return self


class InternalIngressAdapter(InboundAdapter):
    source = "wwebjs"

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        payload = InternalInboundPayload.model_validate(raw_event)
        if payload.source != settings.internal_ingress_source:
            raise ValueError("unsupported internal source")
        is_voice = payload.message_type.casefold() in {"ptt", "audio"}
        metadata = {
            "message_type": payload.message_type,
            "has_media": payload.has_media,
            "input_modality": "voice" if is_voice else "text",
            "media_state": ("PENDING" if payload.has_media else "MISSING") if is_voice else None,
            **payload.metadata,
        }
        kwargs: dict[str, Any] = {}
        if payload.correlation_id:
            kwargs["correlation_id"] = payload.correlation_id
        if payload.received_at:
            kwargs["received_at"] = payload.received_at
        return NormalizedInboundEvent(
            schema_version="1",
            source=payload.source,
            external_event_id=payload.external_event_id,
            event_type=payload.event_type,
            occurred_at=payload.occurred_at,
            actor_id=payload.external_actor_id,
            actor_display_name="Unknown actor",
            actor_category="unknown",
            channel=payload.channel,
            content="" if is_voice else payload.content,
            metadata=metadata,
            raw_reference=f"{payload.source}:{payload.external_event_id}",
            event_origin=payload.event_origin,
            owner_authenticated=payload.owner_authenticated,
            **kwargs,
        )

    def normalize_many(self, raw_event: dict[str, Any]) -> list[NormalizedInboundEvent]:
        return [self.normalize(raw_event)]
