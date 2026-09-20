from datetime import UTC, datetime
import json

import httpx
import pytest

from attention_router.contracts.integration import (
    ExternalActorRef,
    ExternalThreadRef,
    InboundIntegrationEvent,
    IntegrationKind,
    IntegrationSource,
    IntegrationThreadKind,
)
from attention_router.integrations.http_client import (
    IntegrationIngressClientError,
    NeutralIntegrationIngressClient,
    serialize_inbound_event,
)


def _event() -> InboundIntegrationEvent:
    stamp = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    return InboundIntegrationEvent(
        tenant_id="tenant-1",
        source=IntegrationSource(
            kind=IntegrationKind.CHANNEL,
            name="channel.email",
            instance_id="gmail-1",
            account_id="account-1",
        ),
        external_event_id="message-1",
        event_type="message",
        actor=ExternalActorRef(
            external_actor_id="sender@example.invalid",
            display_name="Sender",
        ),
        thread=ExternalThreadRef(
            external_thread_id="thread-1",
            kind=IntegrationThreadKind.THREAD,
        ),
        payload_type="EMAIL_MESSAGE_REFERENCE",
        payload_ref={"message_ref": "gmail:message-1"},
        occurred_at=stamp,
        received_at=stamp,
        idempotency_key="gmail-1:message-1",
        correlation_id="correlation-1",
    )


def test_serializer_is_deterministic_for_retry_bytes():
    first = serialize_inbound_event(_event())
    second = serialize_inbound_event(_event())

    assert first == second
    payload = json.loads(first)
    assert payload["source"]["name"] == "channel.email"
    assert payload["payload_ref"] == {"message_ref": "gmail:message-1"}


def test_neutral_ingress_client_accepts_202_and_duplicate_200():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(request.content)
        status = 202 if len(calls) == 1 else 200
        return httpx.Response(
            status,
            json={
                "transport_version": "1",
                "status": "accepted" if status == 202 else "duplicate",
                "receipt_id": "receipt-1",
                "admitted_at": "2026-09-20T20:00:01Z",
                "correlation_id": payload["correlation_id"],
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, follow_redirects=False)
    client = NeutralIntegrationIngressClient(
        endpoint="https://router.example/api/v1/ingress/integrations/events",
        credential="synthetic-credential",
        client=http,
    )
    event = _event()
    body = serialize_inbound_event(event)

    accepted = client.send(event, serialized_body=body)
    duplicate = client.send(event, serialized_body=body)

    assert accepted.status == "accepted"
    assert duplicate.status == "duplicate"
    assert accepted.receipt_id == duplicate.receipt_id == "receipt-1"
    assert calls[0].content == calls[1].content == body
    assert calls[0].headers["authorization"] == "Bearer synthetic-credential"
    assert calls[0].headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://router.example/api/v1/ingress/integrations/events",
        "https://user:pass@router.example/api/v1/ingress/integrations/events",
        "https://router.example/api/v1/ingress/integrations/events?tenant=x",
    ],
)
def test_neutral_ingress_client_requires_unambiguous_https_endpoint(endpoint):
    with pytest.raises(
        ValueError,
        match="INTEGRATION_INGRESS_ENDPOINT_MUST_BE_HTTPS",
    ):
        NeutralIntegrationIngressClient(
            endpoint=endpoint,
            credential="synthetic",
        )


def test_neutral_ingress_client_rejects_redirect_and_error_envelope():
    responses = iter(
        [
            httpx.Response(307, headers={"Location": "https://other.example/"}),
            httpx.Response(
                503,
                json={
                    "transport_version": "1",
                    "error_code": "INGRESS_UNAVAILABLE",
                    "request_id": "req-1",
                },
            ),
        ]
    )
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _request: next(responses)),
        follow_redirects=False,
    )
    client = NeutralIntegrationIngressClient(
        endpoint="https://router.example/api/v1/ingress/integrations/events",
        credential="synthetic",
        client=http,
    )

    with pytest.raises(IntegrationIngressClientError) as redirect:
        client.send(_event())
    assert redirect.value.status_code == 307

    with pytest.raises(IntegrationIngressClientError) as unavailable:
        client.send(_event())
    assert unavailable.value.status_code == 503
    assert unavailable.value.error_code == "INGRESS_UNAVAILABLE"


def test_neutral_ingress_client_rejects_mismatched_success_correlation():
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                202,
                json={
                    "transport_version": "1",
                    "status": "accepted",
                    "receipt_id": "receipt-1",
                    "admitted_at": "2026-09-20T20:00:01Z",
                    "correlation_id": "wrong",
                },
            )
        ),
        follow_redirects=False,
    )
    client = NeutralIntegrationIngressClient(
        endpoint="https://router.example/api/v1/ingress/integrations/events",
        credential="synthetic",
        client=http,
    )

    with pytest.raises(
        IntegrationIngressClientError,
        match="INTEGRATION_INGRESS_RESPONSE_INVALID",
    ):
        client.send(_event())
