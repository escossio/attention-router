from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from urllib.parse import urlsplit

import httpx

from attention_router.contracts.integration import InboundIntegrationEvent


@dataclass(frozen=True, slots=True)
class IntegrationIngressReceipt:
    status: str
    receipt_id: str
    admitted_at: datetime
    correlation_id: str


class IntegrationIngressClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def serialize_inbound_event(event: InboundIntegrationEvent) -> bytes:
    """Serialize one V1 event deterministically so retries preserve exact bytes."""

    payload = event.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class NeutralIntegrationIngressClient:
    def __init__(
        self,
        *,
        endpoint: str,
        credential: str,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("INTEGRATION_INGRESS_ENDPOINT_MUST_BE_HTTPS")
        if not credential:
            raise ValueError("INTEGRATION_INGRESS_CREDENTIAL_REQUIRED")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("INTEGRATION_INGRESS_TIMEOUT_OUT_OF_RANGE")

        self.endpoint = endpoint
        self._credential = credential
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "NeutralIntegrationIngressClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def send(
        self,
        event: InboundIntegrationEvent,
        *,
        serialized_body: bytes | None = None,
    ) -> IntegrationIngressReceipt:
        body = serialized_body or serialize_inbound_event(event)
        response = self._client.post(
            self.endpoint,
            content=body,
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )

        if response.status_code not in {200, 202}:
            error_code = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                value = payload.get("error_code")
                if isinstance(value, str):
                    error_code = value
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_REQUEST_REJECTED",
                status_code=response.status_code,
                error_code=error_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID",
                status_code=response.status_code,
            )

        expected_status = "accepted" if response.status_code == 202 else "duplicate"
        if (
            payload.get("transport_version") != "1"
            or payload.get("status") != expected_status
            or not isinstance(payload.get("receipt_id"), str)
            or not payload["receipt_id"]
            or payload.get("correlation_id") != event.correlation_id
            or not isinstance(payload.get("admitted_at"), str)
        ):
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID",
                status_code=response.status_code,
            )
        try:
            admitted_at = datetime.fromisoformat(
                payload["admitted_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID",
                status_code=response.status_code,
            ) from exc
        if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
            raise IntegrationIngressClientError(
                "INTEGRATION_INGRESS_RESPONSE_INVALID",
                status_code=response.status_code,
            )

        return IntegrationIngressReceipt(
            status=expected_status,
            receipt_id=payload["receipt_id"],
            admitted_at=admitted_at,
            correlation_id=payload["correlation_id"],
        )


__all__ = [
    "IntegrationIngressClientError",
    "IntegrationIngressReceipt",
    "NeutralIntegrationIngressClient",
    "serialize_inbound_event",
]
