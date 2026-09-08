from attention_router.adapters.wwebjs_outbound import WwebjsOutboundAdapter
from attention_router.config import settings


class LocalTransportOutboundAdapter(WwebjsOutboundAdapter):
    """Authenticated dispatcher client for the AGT-local transport."""

    def __init__(self):
        super().__init__(
            url=settings.local_transport_outbound_url,
            secret=settings.internal_ingress_hmac_secret,
            timeout=settings.local_transport_outbound_timeout_seconds,
        )

    def build_payload(self, outbox):
        payload = super().build_payload(outbox)
        payload["destination"] = payload["external_actor_id"]
        payload["execution_intent_id"] = outbox.execution_intent_id
        return payload


local_transport_outbound = LocalTransportOutboundAdapter()
