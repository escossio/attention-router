"""Provider adapters that terminate at the neutral Integration Contract."""

from attention_router.integrations.email_connector import (
    EmailConnector,
    EmailConnectorError,
    EmailMessageSnapshot,
    deterministic_message_ref,
    snapshot_rfc822_message,
)
from attention_router.integrations.http_client import (
    IntegrationIngressClient,
    IntegrationIngressClientError,
    IntegrationIngressReceipt,
)
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
    "EmailConnector",
    "EmailConnectorError",
    "EmailMessageSnapshot",
    "IntegrationIngressClient",
    "IntegrationIngressClientError",
    "IntegrationIngressReceipt",
    "WhatsAppNormalizedAdapter",
    "deterministic_message_ref",
    "snapshot_rfc822_message",
]
