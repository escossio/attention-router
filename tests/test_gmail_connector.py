from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
import json
from urllib import error as urllib_error

import pytest

from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorConfig,
    GmailConnectorError,
    GmailInboundConnector,
    GmailMessage,
    IntegrationIngressClient,
    gmail_message_to_normalized_input,
)


class FakeReader:
    def __init__(self, messages):
        self.messages = {message.message_id: message for message in messages}
        self.queries = []
        self.reads = []

    def search_message_ids(self, *, query, max_results):
        self.queries.append((query, max_results))
        return tuple(self.messages)[:max_results]

    def read_message(self, message_id):
        self.reads.append(message_id)
        return self.messages[message_id]


class FakeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body


def _message(
    message_id="gmail-message-1",
    *,
    sender="Synthetic Sender <sender@example.invalid>",
    email_ts="2026-09-20T19:00:00-03:00",
    attachments=(),
):
    return GmailMessage(
        message_id=message_id,
        thread_id="gmail-thread-1",
        sender=sender,
        to=("owner@example.invalid",),
        cc=(),
        bcc=(),
        subject="Synthetic subject",
        body="provider private body",
        email_ts=email_ts,
        attachments=attachments,
    )


def _config():
    return GmailConnectorConfig(
        tenant_id="00000000-0000-4000-8000-000000000001",
        instance_id="gmail-primary",
        account_id="owner@example.invalid",
        ingress_url="https://router.invalid/api/v1/ingress/integrations/events",
        ingress_bearer="A" * 43,
    )


def test_gmail_message_mapping_accepts_connector_timestamp_shapes():
    offset = gmail_message_to_normalized_input(_message())
    assert offset["from"] == {
        "address": "sender@example.invalid",
        "name": "Synthetic Sender",
    }
    assert offset["sent_at"] == datetime(
        2026, 9, 20, 22, 0, tzinfo=UTC
    )

    utc_wall = gmail_message_to_normalized_input(
        _message(email_ts="2026-09-20T22:00:00")
    )
    assert utc_wall["sent_at"] == datetime(
        2026, 9, 20, 22, 0, tzinfo=UTC
    )


def test_gmail_connector_never_serializes_message_body_into_v1_contract():
    sent = []

    def opener(request, timeout):
        sent.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": request.data,
                "timeout": timeout,
            }
        )
        return FakeHTTPResponse(
            202,
            {
                "transport_version": "1",
                "status": "accepted",
                "receipt_id": "receipt-1",
                "admitted_at": "2026-09-20T22:00:01Z",
                "correlation_id": "corr-1",
            },
        )

    ingress = IntegrationIngressClient(
        url=_config().ingress_url,
        bearer=_config().ingress_bearer,
        opener=opener,
    )
    connector = GmailInboundConnector(
        reader=FakeReader([_message()]),
        ingress=ingress,
        config=_config(),
    )

    response = connector.ingest_message(_message())

    assert response.status_code == 202
    assert response.body["status"] == "accepted"
    assert len(sent) == 1
    payload = json.loads(sent[0]["body"])
    assert payload["contract_type"] == "inbound_event"
    assert payload["schema_version"] == "1"
    assert payload["tenant_id"] == _config().tenant_id
    assert payload["source"] == {
        "kind": "CHANNEL",
        "name": "channel.email",
        "instance_id": "gmail-primary",
        "account_id": "owner@example.invalid",
    }
    assert payload["external_event_id"] == "gmail-message-1"
    assert payload["actor"]["external_actor_id"] == "sender@example.invalid"
    assert payload["thread"]["external_thread_id"] == "gmail-thread-1"
    assert payload["payload_type"] == "EMAIL_MESSAGE_REFERENCE"
    assert payload["payload_ref"] == {
        "message_ref": "gmail:gmail-message-1"
    }
    assert payload["metadata_sanitized"]["body_present"] is True
    assert "provider private body" not in sent[0]["body"].decode()
    assert "Synthetic subject" not in sent[0]["body"].decode()
    assert sent[0]["headers"]["Authorization"] == (
        "Bearer " + _config().ingress_bearer
    )


def test_gmail_connector_poll_treats_200_duplicate_as_success():
    messages = [_message("gmail-1"), _message("gmail-2")]
    reader = FakeReader(messages)
    statuses = iter(
        [
            (
                202,
                {
                    "transport_version": "1",
                    "status": "accepted",
                    "receipt_id": "r1",
                    "admitted_at": "2026-09-20T22:00:01Z",
                    "correlation_id": "c1",
                },
            ),
            (
                200,
                {
                    "transport_version": "1",
                    "status": "duplicate",
                    "receipt_id": "r2",
                    "admitted_at": "2026-09-20T22:00:01Z",
                    "correlation_id": "c2",
                },
            ),
        ]
    )

    def opener(_request, timeout):
        status, body = next(statuses)
        return FakeHTTPResponse(status, body)

    connector = GmailInboundConnector(
        reader=reader,
        ingress=IntegrationIngressClient(
            url=_config().ingress_url,
            bearer=_config().ingress_bearer,
            opener=opener,
        ),
        config=_config(),
    )

    result = connector.poll(
        query="newer_than:1d -in:spam -in:trash",
        max_results=2,
    )

    assert result.selected == 2
    assert result.accepted == 1
    assert result.duplicates == 1
    assert reader.queries == [
        ("newer_than:1d -in:spam -in:trash", 2)
    ]
    assert reader.reads == ["gmail-1", "gmail-2"]


def test_gmail_connector_fails_closed_on_attachments_until_artifact_plane():
    connector = GmailInboundConnector(
        reader=FakeReader([]),
        ingress=IntegrationIngressClient(
            url=_config().ingress_url,
            bearer=_config().ingress_bearer,
            opener=lambda *_args, **_kwargs: pytest.fail(
                "ingress must not be called"
            ),
        ),
        config=_config(),
    )
    message = _message(
        attachments=(
            GmailAttachmentSummary(
                attachment_id="provider-attachment-id",
                filename="synthetic.png",
                mime_type="image/png",
                size_bytes=123,
            ),
        )
    )

    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_ATTACHMENTS_REQUIRE_ARTIFACT_PLANE",
    ):
        connector.ingest_message(message)


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (
            401,
            {"error_code": "UNAUTHENTICATED"},
            "UNAUTHENTICATED",
        ),
        (
            409,
            {"error_code": "IDEMPOTENCY_CONFLICT"},
            "IDEMPOTENCY_CONFLICT",
        ),
        (
            503,
            {"error_code": "INGRESS_UNAVAILABLE"},
            "INGRESS_UNAVAILABLE",
        ),
    ],
)
def test_ingress_client_surfaces_bounded_server_rejection(
    status,
    body,
    reason,
):
    def opener(request, timeout):
        raise urllib_error.HTTPError(
            request.full_url,
            status,
            reason,
            {},
            BytesIO(json.dumps(body).encode()),
        )

    client = IntegrationIngressClient(
        url=_config().ingress_url,
        bearer=_config().ingress_bearer,
        opener=opener,
    )

    with pytest.raises(GmailConnectorError, match=reason):
        client.send({"contract_type": "inbound_event"})


def test_gmail_connector_rejects_invalid_sender_and_poll_limit():
    connector = GmailInboundConnector(
        reader=FakeReader([]),
        ingress=IntegrationIngressClient(
            url=_config().ingress_url,
            bearer=_config().ingress_bearer,
        ),
        config=_config(),
    )

    with pytest.raises(GmailConnectorError, match="GMAIL_SENDER_INVALID"):
        gmail_message_to_normalized_input(
            _message(sender="not-an-address")
        )

    with pytest.raises(ValueError, match="GMAIL_POLL_LIMIT_OUT_OF_RANGE"):
        connector.poll(max_results=0)
