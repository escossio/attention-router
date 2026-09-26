from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
import io
import json
import urllib.error

import pytest

from attention_router.integrations.email_connector import (
    EmailConnector,
    EmailConnectorError,
    deterministic_message_ref,
    snapshot_rfc822_message,
)
from attention_router.integrations.http_client import (
    IntegrationIngressClient,
    IntegrationIngressClientError,
)


TOKEN = "A" * 43
NOW = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)


def _message(
    *,
    message_id="<mail-1@example.invalid>",
    subject="Synthetic subject",
    body="Synthetic body",
):
    raw = (
        f"From: Synthetic Sender <sender@example.invalid>\r\n"
        f"To: Owner <owner@example.invalid>\r\n"
        f"Date: Sun, 20 Sep 2026 22:00:00 +0000\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Subject: {subject}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}"
    ).encode()
    return BytesParser(policy=policy.default).parsebytes(raw)


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


def test_rfc822_snapshot_extracts_metadata_without_body_content():
    message = _message(body="TOP SECRET BODY CONTENT")

    snapshot = snapshot_rfc822_message(
        message,
        message_ref="email-ref:opaque",
        thread_id="provider-thread-1",
    )

    assert snapshot.message_id == "<mail-1@example.invalid>"
    assert snapshot.thread_id == "provider-thread-1"
    assert snapshot.sender_address == "sender@example.invalid"
    assert snapshot.sender_name == "Synthetic Sender"
    assert snapshot.sent_at == NOW
    assert snapshot.subject_present is True
    assert snapshot.body_present is True
    assert snapshot.recipient_count == 1
    assert snapshot.message_ref == "email-ref:opaque"
    assert "TOP SECRET" not in repr(snapshot)


def test_email_connector_normalizes_and_sends_reference_only_event():
    captured = {}

    def opener(request, *, timeout):
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        payload = json.loads(request.data.decode())
        captured["payload"] = payload
        return _Response(
            202,
            {
                "transport_version": "1",
                "status": "accepted",
                "receipt_id": "receipt-1",
                "admitted_at": "2026-09-20T22:00:01Z",
                "correlation_id": payload["correlation_id"],
            },
        )

    ingress = IntegrationIngressClient(
        endpoint="https://router.example.invalid/api/v1/ingress/integrations/events",
        bearer=TOKEN,
        opener=opener,
    )
    connector = EmailConnector(
        tenant_id="tenant-a",
        instance_id="mailbox-1",
        account_id="owner@example.invalid",
        ingress=ingress,
    )

    receipt = connector.ingest(
        _message(body="DO NOT COPY THIS BODY"),
        message_ref=deterministic_message_ref("mailbox-1", "provider-id-1"),
        thread_id="thread-provider-1",
        received_at=NOW,
    )

    assert receipt.status == "accepted"
    assert captured["timeout"] == 10.0
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"
    payload = captured["payload"]
    assert payload["source"] == {
        "kind": "CHANNEL",
        "name": "channel.email",
        "instance_id": "mailbox-1",
        "account_id": "owner@example.invalid",
    }
    assert payload["actor"]["external_actor_id"] == "sender@example.invalid"
    assert payload["thread"]["external_thread_id"] == "thread-provider-1"
    assert payload["payload_type"] == "EMAIL_MESSAGE_REFERENCE"
    assert payload["artifact_ids"] == []
    assert payload["metadata_sanitized"]["body_present"] is True
    assert payload["metadata_sanitized"]["attachment_count"] == 0
    rendered = captured["body"].decode()
    assert "DO NOT COPY THIS BODY" not in rendered
    assert "Synthetic subject" not in rendered


def test_ingress_client_accepts_duplicate_receipt():
    event_holder = {}

    def opener(request, *, timeout):
        payload = json.loads(request.data.decode())
        event_holder.update(payload)
        return _Response(
            200,
            {
                "transport_version": "1",
                "status": "duplicate",
                "receipt_id": "receipt-existing",
                "admitted_at": "2026-09-20T22:00:01Z",
                "correlation_id": payload["correlation_id"],
            },
        )

    client = IntegrationIngressClient(
        endpoint="https://router.example.invalid/ingress",
        bearer=TOKEN,
        opener=opener,
    )
    connector = EmailConnector(
        tenant_id="tenant-a",
        instance_id="mailbox-1",
        account_id=None,
        ingress=client,
    )

    receipt = connector.ingest(
        _message(),
        message_ref="email-ref:opaque",
        received_at=NOW,
    )

    assert receipt.status == "duplicate"
    assert receipt.receipt_id == "receipt-existing"


def test_ingress_client_fails_closed_on_wrong_correlation_response():
    def opener(request, *, timeout):
        return _Response(
            202,
            {
                "transport_version": "1",
                "status": "accepted",
                "receipt_id": "receipt-1",
                "admitted_at": "2026-09-20T22:00:01Z",
                "correlation_id": "wrong",
            },
        )

    connector = EmailConnector(
        tenant_id="tenant-a",
        instance_id="mailbox-1",
        account_id=None,
        ingress=IntegrationIngressClient(
            endpoint="https://router.example.invalid/ingress",
            bearer=TOKEN,
            opener=opener,
        ),
    )

    with pytest.raises(
        IntegrationIngressClientError,
        match="INTEGRATION_INGRESS_RESPONSE_INVALID",
    ):
        connector.ingest(
            _message(),
            message_ref="email-ref:opaque",
            received_at=NOW,
        )


def test_ingress_client_redacts_server_body_and_bearer_from_errors():
    secret_response = b'{"detail":"provider secret should not escape"}'

    def opener(_request, *, timeout):
        raise urllib.error.HTTPError(
            "https://router.example.invalid/ingress",
            403,
            "Forbidden",
            {},
            io.BytesIO(secret_response),
        )

    client = IntegrationIngressClient(
        endpoint="https://router.example.invalid/ingress",
        bearer=TOKEN,
        opener=opener,
    )
    connector = EmailConnector(
        tenant_id="tenant-a",
        instance_id="mailbox-1",
        account_id=None,
        ingress=client,
    )

    with pytest.raises(IntegrationIngressClientError) as exc:
        connector.ingest(
            _message(),
            message_ref="email-ref:opaque",
            received_at=NOW,
        )

    assert str(exc.value) == "INTEGRATION_INGRESS_HTTP_403"
    assert TOKEN not in str(exc.value)
    assert "provider secret" not in str(exc.value)


@pytest.mark.parametrize(
    "raw,reason",
    [
        (
            b"From: sender@example.invalid\r\n"
            b"Date: Sun, 20 Sep 2026 22:00:00 +0000\r\n\r\nbody",
            "EMAIL_MESSAGE_ID_REQUIRED",
        ),
        (
            b"From: sender@example.invalid\r\n"
            b"Message-ID: <x@example.invalid>\r\n\r\nbody",
            "EMAIL_DATE_REQUIRED",
        ),
        (
            b"Date: Sun, 20 Sep 2026 22:00:00 +0000\r\n"
            b"Message-ID: <x@example.invalid>\r\n\r\nbody",
            "EMAIL_SENDER_REQUIRED",
        ),
    ],
)
def test_email_snapshot_fails_closed_on_missing_identity_fields(raw, reason):
    message = BytesParser(policy=policy.default).parsebytes(raw)

    with pytest.raises(EmailConnectorError, match=reason):
        snapshot_rfc822_message(
            message,
            message_ref="email-ref:opaque",
        )


def test_deterministic_message_ref_is_opaque_and_stable():
    first = deterministic_message_ref("mailbox-1", "provider-message-1")
    second = deterministic_message_ref("mailbox-1", "provider-message-1")
    other = deterministic_message_ref("mailbox-1", "provider-message-2")

    assert first == second
    assert first != other
    assert "provider-message-1" not in first
    assert first.startswith("email-ref:")
