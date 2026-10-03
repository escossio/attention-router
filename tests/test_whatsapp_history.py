from __future__ import annotations

from datetime import datetime, UTC
import hashlib
import hmac
import json
from urllib import parse as urllib_parse

import pytest

from attention_router.integrations.whatsapp_history import (
    LocalWhatsAppHistoryAdapter,
    WhatsAppHistoryError,
)


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, limit=None):
        return self._body[:limit]


def test_adapter_signs_exact_path_and_query_and_maps_history():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        parsed = urllib_parse.urlsplit(request.full_url)
        if parsed.path == "/internal/history/chats":
            return FakeResponse(
                200,
                {
                    "status": "ok",
                    "chats": [
                        {
                            "external_thread_key": "person@c.us",
                            "thread_type": "DIRECT",
                            "title": "Pessoa",
                        }
                    ],
                },
            )
        return FakeResponse(
            200,
            {
                "status": "ok",
                "messages": [
                    {
                        "source_message_id": "message-1",
                        "external_thread_key": "person@c.us",
                        "external_sender_key": "person@c.us",
                        "from_me": False,
                        "timestamp": "2026-10-03T12:00:00.000Z",
                        "type": "chat",
                        "text": "Oi",
                        "has_media": False,
                        "reply_reference": None,
                        "reply_reference_available": False,
                    }
                ],
                "next_cursor": "cursor-2",
            },
        )

    adapter = LocalWhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="history-secret",
        opener=opener,
        clock=lambda: 1234,
    )
    assert adapter.list_chats()[0]["external_thread_key"] == "person@c.us"
    page = adapter.fetch_messages("person@c.us", 20, "cursor-1")

    assert page["next_cursor"] == "cursor-2"
    assert page["messages"][0]["sent_at"] == datetime(
        2026, 10, 3, 12, 0, tzinfo=UTC
    )
    assert page["messages"][0]["type"] == "TEXT"

    request, timeout = calls[-1]
    parsed = urllib_parse.urlsplit(request.full_url)
    target = parsed.path + "?" + parsed.query
    expected = hmac.new(
        b"history-secret",
        f"1234.GET.{target}".encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {key.casefold(): value for key, value in request.header_items()}
    assert headers["x-attention-timestamp"] == "1234"
    assert headers["x-attention-signature"] == f"sha256={expected}"
    assert timeout == 10.0
    assert parsed.path.endswith("/person%40c.us")
    assert urllib_parse.parse_qs(parsed.query) == {
        "limit": ["20"],
        "cursor": ["cursor-1"],
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "WHATSAPP_HISTORY_CHAT_NOT_FOUND"),
        (409, "WHATSAPP_HISTORY_CURSOR_STALE"),
        (413, "WHATSAPP_HISTORY_SCAN_LIMIT_EXCEEDED"),
        (503, "WHATSAPP_HISTORY_UNAVAILABLE"),
    ],
)
def test_adapter_maps_transport_failures_without_response_body(status, code):
    adapter = LocalWhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="history-secret",
        opener=lambda _request, timeout: FakeResponse(
            status, {"status": "history_read_failed", "sensitive": "ignored"}
        ),
    )

    with pytest.raises(WhatsAppHistoryError, match=code):
        adapter.fetch_messages("person@c.us", 10)


def test_adapter_rejects_malformed_message_and_oversized_response():
    bad = LocalWhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="history-secret",
        opener=lambda _request, timeout: FakeResponse(
            200,
            {
                "status": "ok",
                "messages": [
                    {
                        "source_message_id": "m1",
                        "timestamp": "not-a-time",
                    }
                ],
                "next_cursor": None,
            },
        ),
    )
    with pytest.raises(
        WhatsAppHistoryError,
        match="WHATSAPP_HISTORY_MESSAGE_INVALID",
    ):
        bad.fetch_messages("person@c.us", 10)

    huge = LocalWhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="history-secret",
        max_response_bytes=1024,
        opener=lambda _request, timeout: FakeResponse(
            200,
            {"status": "ok", "messages": [], "padding": "x" * 5000},
        ),
    )
    with pytest.raises(
        WhatsAppHistoryError,
        match="WHATSAPP_HISTORY_RESPONSE_TOO_LARGE",
    ):
        huge.fetch_messages("person@c.us", 10)


def test_adapter_rejects_unsafe_configuration_and_unbounded_cursor():
    with pytest.raises(ValueError, match="WHATSAPP_HISTORY_URL_INVALID"):
        LocalWhatsAppHistoryAdapter(
            base_url="file:///tmp/history",
            hmac_secret="secret",
        )
    with pytest.raises(
        ValueError,
        match="WHATSAPP_HISTORY_HMAC_SECRET_REQUIRED",
    ):
        LocalWhatsAppHistoryAdapter(
            base_url="http://127.0.0.1:18103/internal/history/chats",
            hmac_secret="",
        )

    adapter = LocalWhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="secret",
        opener=lambda *_args, **_kwargs: pytest.fail("HTTP must not be called"),
    )
    with pytest.raises(ValueError, match="WHATSAPP_HISTORY_CURSOR_INVALID"):
        adapter.fetch_messages("person@c.us", 10, "x" * 4097)
