"""Provider adapters that terminate at the neutral Integration Contract."""

from attention_router.integrations.channel_adapters import (
    AdapterNormalizationError,
    ChannelAdapterOutput,
    ChannelInboundAdapter,
    EmailNormalizedAdapter,
    WhatsAppNormalizedAdapter,
)

__all__ = [
    "AdapterNormalizationError",
    "ChannelAdapterOutput",
    "ChannelInboundAdapter",
    "EmailNormalizedAdapter",
    "WhatsAppNormalizedAdapter",
]
