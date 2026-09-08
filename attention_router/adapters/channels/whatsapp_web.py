from __future__ import annotations

from attention_router.core.channels import ChannelKind, ChannelTarget


class WhatsAppWebChannelBoundary:
    """Keeps JID/LID details in the adapter boundary; core uses binding IDs only."""

    kind = ChannelKind.WHATSAPP_WEB

    @staticmethod
    def target(binding_id: str) -> ChannelTarget:
        return ChannelTarget(binding_id=binding_id, channel=ChannelKind.WHATSAPP_WEB)

    @staticmethod
    def accepts_source(source: str) -> bool:
        return source == "wwebjs"
