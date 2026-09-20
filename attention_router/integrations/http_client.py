from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.request
from typing import Callable

from attention_router.contracts.integration import InboundIntegrationEvent
from attention_router.integrations.tenant_binding import credential_digest


class IntegrationIngressClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntegrationIngressReceipt:
    status: str
    receipt_id: str
    admitted_at: str
    correlation_id: str


class IntegrationIngressClient:
    """Thin connector-side client for the neutral inbound integration boundary."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer: str,
        timeout_seconds: float = 10.0,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("INTEGRATION_INGRESS_ENDPOINT_INVALID")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("INTEGRATION_INGRESS_TIMEOUT_INVALID")
        # Validate representation only. Entropy/provisioning remain server-side
        # administrative responsibilities.
        credential_digest(bearer)
        self._endpoint = endpoint.rstrip("/")
        self._bearer = bearer
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def send(self, event: InboundIntegrationEvent) -> IntegrationIngressReceipt:
        body = event.model_dump_json(
            exclude_none=False,
            by_alias=True,
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._bearer}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            response = self._opener(
                request,
                timeout=self._timeout_seconds,
            )
            with response:
                status_code = int(response.status)
                raw = response.read(65_537)
        except urllib.error.HTTPError as exc:
            # Never include response bodies or bearer material in connector
            # exceptions. The server's stable status is enough for policy.
            raise IntegrationIngressClientError(
                f"INTEGRATION_INGRESS_HTTP_{exc.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_UNAVAILABLE"
            ) from None

        if status_code not in {200, 202}:
            raise IntegrationIngressClientError(
                f"INTEGRATION_INGRESS_HTTP_{status_code}"
            )
        if len(raw) > 65_536:
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_TOO_LARGE"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID"
            ) from None
        if not isinstance(payload, dict):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID"
            )

        expected_status = "duplicate" if status_code == 200 else "accepted"
        if (
            payload.get("transport_version") != "1"
            or payload.get("status") != expected_status
            or not isinstance(payload.get("receipt_id"), str)
            or not payload["receipt_id"]
            or not isinstance(payload.get("admitted_at"), str)
            or not payload["admitted_at"]
            or payload.get("correlation_id") != event.correlation_id
        ):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID"
            )
        return IntegrationIngressReceipt(
            status=payload["status"],
            receipt_id=payload["receipt_id"],
            admitted_at=payload["admitted_at"],
            correlation_id=payload["correlation_id"],
        )


__all__ = [
    "IntegrationIngressClient",
    "IntegrationIngressClientError",
    "IntegrationIngressReceipt",
]
