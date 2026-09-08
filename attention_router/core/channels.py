from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from attention_router.core.events import EventEnvelope


class ChannelKind(StrEnum):
    WHATSAPP_WEB = "WHATSAPP_WEB"
    WHATSAPP_META = "WHATSAPP_META"
    MOBILE_APP = "MOBILE_APP"
    WEB = "WEB"
    VOICE = "VOICE"
    EMAIL = "EMAIL"
    TEAMS = "TEAMS"


@dataclass(frozen=True)
class ChannelTarget:
    binding_id: str
    channel: ChannelKind


@dataclass(frozen=True)
class ChannelDeliveryRequest:
    target: ChannelTarget
    payload_type: str
    payload: dict[str, Any]
    idempotency_key: str


class ChannelAdapter(Protocol):
    kind: ChannelKind

    def normalize(self, raw_event: dict[str, Any]) -> EventEnvelope: ...

    def deliver(self, request: ChannelDeliveryRequest) -> dict[str, Any]: ...
