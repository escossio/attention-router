"""Provider-agnostic contracts for the Attention Router platform core."""

from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.events import EventEnvelope, EventOrigin
from attention_router.core.tenancy import DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG

__all__ = [
    "CapabilityRequest",
    "DEFAULT_TENANT_ID",
    "DEFAULT_TENANT_SLUG",
    "EventEnvelope",
    "EventOrigin",
]
