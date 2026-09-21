from __future__ import annotations

from io import BytesIO
import json
from urllib import error as urllib_error
from urllib import parse as urllib_parse

import pytest

from attention_router.integrations.gmail_api_reader import (
    GMAIL_API_BASE,
    GMAIL_METADATA_HEADERS,
    GmailApiReader,
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorError,
)


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self, limit=None):
        return self._body[:limit]


def _full_message(*, attachment=False):
    parts = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "filename": "",
            "body": {
                "size": 12,
                "data": "c3ludGhldGlj",
            },
        }
    ]
    if attachment:
        parts.append(
            {
                "partId": "1",
                "mimeType": "image/png",
                "filename": "synthetic.png",
                "body": {
                    "attachmentId": "provider-attachment-1",
                    "size": 1234,
                },
            }
        )
    return {
        "id": "gmail-message-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["INBOX"],
        "snippet": "synthetic snippet",
        "internalDate": "1789941600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": [
                {
                    "name": "From",
                    "value": "Synthetic Sender <sender@example.invalid>",
                },
                {
                    "name": "To",
                    "value": (
                        '"Doe, John" <john@example.invalid>, '
                        "jane@example.invalid"
                    ),
                },
                {"name": "Cc", "value": "copy@example.invalid"},
                {"name": "Subject", "value": "Synthetic subject"},
                {
                    "name": "Date",
                    "value": "Sun, 20 Sep 2026 19:00:00 -0300",
                },
            ],
            "body": {"size": 0},
            "parts": parts,
        },
    }


def test_reader_lists_ids_with_read_only_query_shape():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(
            200,
            {
                "messages": [
                    {"id": "a", "threadId": "ta"},
                    {"id": "b", "threadId": "tb"},
                ]
            },
        )

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token-123"),
        opener=opener,
    )

    ids = reader.search_message_ids(
        query="",
        max_results=2,
    )

    assert ids == ("a", "b")
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.get_method() == "GET"
    assert request.full_url.startswith(GMAIL_API_BASE + "/messages?")
    assert "labelIds=INBOX" in request.full_url
    assert "maxResults=2" in request.full_url
    assert "includeSpamTrash=false" in request.full_url
    assert "q=" not in request.full_url
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.headers["Accept"] == "application/json"
    assert timeout == 10.0


def test_reader_rejects_search_query_under_metadata_scope():
    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token-123"),
        opener=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
    )

    with pytest.raises(GmailConnectorError, match="GMAIL_METADATA_QUERY_FORBIDDEN"):
        reader.search_message_ids(query="newer_than:1d", max_results=1)


def test_reader_reads_metadata_without_body_or_attachment_observation():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(200, _full_message(attachment=True))

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token-123"),
        opener=opener,
    )

    message = reader.read_message("gmail-message-1")

    assert message.message_id == "gmail-message-1"
    assert message.thread_id == "gmail-thread-1"
    assert message.sender == "Synthetic Sender <sender@example.invalid>"
    assert message.to == (
        "john@example.invalid",
        "jane@example.invalid",
    )
    assert message.cc == ("copy@example.invalid",)
    assert message.bcc == ()
    assert message.subject == "Synthetic subject"
    assert message.body == ""
    assert message.email_ts.endswith("+00:00")
    assert message.attachments == ()
    assert message.body_observed is False
    assert message.attachments_observed is False

    assert len(calls) == 1
    assert calls[0].startswith(
        GMAIL_API_BASE + "/messages/gmail-message-1?format=metadata"
    )
    for header in GMAIL_METADATA_HEADERS:
        assert f"metadataHeaders={header}" in calls[0]
    assert "/attachments/" not in calls[0]


def test_reader_prefers_internal_date_over_date_header():
    payload = _full_message()
    payload["internalDate"] = "0"
    payload["payload"]["headers"] = [
        header
        for header in payload["payload"]["headers"]
        if header["name"] != "Date"
    ] + [
        {
            "name": "Date",
            "value": "Sun, 20 Sep 2026 19:00:00 -0300",
        }
    ]

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=lambda _request, timeout: FakeResponse(200, payload),
    )

    message = reader.read_message("gmail-message-1")

    assert message.email_ts == "1970-01-01T00:00:00+00:00"


def test_reader_falls_back_to_rfc_date_header_when_internal_date_missing():
    payload = _full_message()
    del payload["internalDate"]

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=lambda _request, timeout: FakeResponse(200, payload),
    )

    message = reader.read_message("gmail-message-1")

    assert message.email_ts == "2026-09-20T22:00:00+00:00"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "GMAIL_API_UNAUTHENTICATED"),
        (403, "GMAIL_API_UNAUTHENTICATED"),
        (429, "GMAIL_API_UNAVAILABLE"),
        (500, "GMAIL_API_UNAVAILABLE"),
        (404, "GMAIL_API_REJECTED"),
    ],
)
def test_reader_maps_provider_errors_without_leaking_response(
    status,
    reason,
):
    def opener(request, timeout):
        raise urllib_error.HTTPError(
            request.full_url,
            status,
            "private provider error",
            {},
            BytesIO(b'{"error":{"message":"private body"}}'),
        )

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("super-secret-token"),
        opener=opener,
    )

    with pytest.raises(GmailConnectorError, match=reason) as exc:
        reader.read_message("gmail-message-1")

    rendered = str(exc.value)
    assert "super-secret-token" not in rendered
    assert "private provider error" not in rendered
    assert "private body" not in rendered


def test_reader_rejects_invalid_provider_shapes():
    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=lambda _request, timeout: FakeResponse(
            200,
            {"messages": [{"threadId": "missing-id"}]},
        ),
    )
    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_API_RESPONSE_INVALID",
    ):
        reader.search_message_ids(query="", max_results=1)

    broken_message = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=lambda _request, timeout: FakeResponse(
            200,
            {
                "id": "gmail-message-1",
                "payload": {"headers": []},
            },
        ),
    )
    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_API_SENDER_MISSING",
    ):
        broken_message.read_message("gmail-message-1")


def test_static_token_provider_never_accepts_blank_token():
    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_ACCESS_TOKEN_UNAVAILABLE",
    ):
        StaticGmailAccessTokenProvider(" ").access_token()


def _attachment_structure(*, include_forbidden_data=False):
    attachment_body = {
        "attachmentId": "provider-attachment-1",
        "size": 5,
    }
    if include_forbidden_data:
        attachment_body["data"] = "aGVsbG8"
    return {
        "payload": {
            "mimeType": "multipart/mixed",
            "filename": "",
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "body": {"size": 12},
                },
                {
                    "mimeType": "image/png",
                    "filename": "synthetic.png",
                    "body": attachment_body,
                },
            ],
        }
    }


def test_reader_discovers_attachment_structure_without_body_data():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if "format=metadata" in request.full_url:
            return FakeResponse(200, _full_message(attachment=True))
        return FakeResponse(200, _attachment_structure())

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token-123"),
        opener=opener,
    )
    message = reader.read_message_with_attachments(
        "gmail-message-1",
        max_attachments=4,
        max_mime_depth=4,
    )

    assert message.body == ""
    assert message.body_observed is False
    assert message.attachments_observed is True
    assert message.attachments == (
        GmailAttachmentSummary(
            attachment_id="provider-attachment-1",
            filename="synthetic.png",
            mime_type="image/png",
            size_bytes=5,
        ),
    )
    assert len(calls) == 2
    structure_url = calls[1]
    assert "format=full" in structure_url
    decoded_url = urllib_parse.unquote(structure_url)
    assert "snippet" not in decoded_url
    assert "body(data" not in decoded_url
    assert "body(attachmentId,size)" in decoded_url

def test_reader_downloads_bounded_base64url_attachment():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return FakeResponse(
            200,
            {"size": 5, "data": "aGVsbG8"},
        )

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token-123"),
        opener=opener,
    )
    data = reader.read_attachment(
        "gmail-message-1",
        "provider-attachment-1",
        expected_size=5,
        max_bytes=64,
    )

    assert data == b"hello"
    assert len(calls) == 1
    assert (
        "/messages/gmail-message-1/attachments/provider-attachment-1"
        in calls[0]
    )
    assert "fields=size%2Cdata" in calls[0]


def test_reader_rejects_provider_body_data_during_attachment_discovery():
    def opener(request, timeout):
        if "format=metadata" in request.full_url:
            return FakeResponse(200, _full_message(attachment=True))
        return FakeResponse(
            200,
            _attachment_structure(include_forbidden_data=True),
        )

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=opener,
    )

    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_MESSAGE_BODY_DATA_FORBIDDEN",
    ):
        reader.read_message_with_attachments(
            "gmail-message-1",
            max_attachments=4,
            max_mime_depth=4,
        )

@pytest.mark.parametrize(
    "payload",
    [
        {"size": 4, "data": "aGVsbG8"},
        {"size": 5, "data": "***not-base64url***"},
        {"size": "5", "data": "aGVsbG8"},
    ],
)
def test_reader_rejects_invalid_attachment_payload(payload):
    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider("token"),
        opener=lambda _request, timeout: FakeResponse(200, payload),
    )

    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_ATTACHMENT_",
    ):
        reader.read_attachment(
            "gmail-message-1",
            "provider-attachment-1",
            expected_size=5,
            max_bytes=64,
        )
