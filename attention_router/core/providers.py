from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


class ProviderState(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED = "CONFIGURED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    SUSPENDED = "SUSPENDED"


class ProviderHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ProviderResult:
    success: bool
    result: dict[str, Any] = field(default_factory=dict)
    reason_code: str = "OK"


@runtime_checkable
class CapabilityProvider(Protocol):
    interface_name: str

    def health(self) -> str: ...

    def execute(self, capability: str, parameters: Mapping[str, Any]) -> ProviderResult: ...


class ObjectStorageProvider(Protocol):
    def put_object(self, object_ref: str, content_ref: str) -> ProviderResult: ...

    def get_object(self, object_ref: str) -> ProviderResult: ...


class PaymentProvider(Protocol):
    def verify(self, payment_ref: str) -> ProviderResult: ...

    def create_charge(self, parameters: Mapping[str, Any]) -> ProviderResult: ...


class LocationProvider(Protocol):
    def current_location(self, subject_ref: str) -> ProviderResult: ...


class CalendarProvider(Protocol):
    def availability(self, parameters: Mapping[str, Any]) -> ProviderResult: ...

    def schedule(self, parameters: Mapping[str, Any]) -> ProviderResult: ...


class VehicleProvider(Protocol):
    def locate(self, vehicle_ref: str) -> ProviderResult: ...


class CRMProvider(Protocol):
    def lookup_contact(self, contact_ref: str) -> ProviderResult: ...


class NotificationProvider(Protocol):
    def notify(self, target_ref: str, payload_ref: str) -> ProviderResult: ...


class IdentityProvider(Protocol):
    def verify_identity(self, identity_ref: str) -> ProviderResult: ...


class DeviceProvider(Protocol):
    def invoke_device_capability(
        self,
        device_ref: str,
        capability: str,
        parameters: Mapping[str, Any],
    ) -> ProviderResult: ...


class ProviderRuntimeRegistry:
    """Process-local implementations; PostgreSQL remains the binding source of truth."""

    def __init__(self) -> None:
        self._providers: dict[str, CapabilityProvider] = {}

    def register(self, provider_instance_id: str, provider: CapabilityProvider) -> None:
        self._providers[provider_instance_id] = provider

    def resolve(self, provider_instance_id: str, interface_name: str) -> CapabilityProvider | None:
        provider = self._providers.get(provider_instance_id)
        if provider is None or provider.interface_name != interface_name:
            return None
        return provider
