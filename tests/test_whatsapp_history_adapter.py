from __future__ import annotations

import json
import hashlib
import hmac
from urllib.error import HTTPError, URLError
from urllib import parse as urllib_parse

import pytest

from attention_router.integrations.whatsapp_history import (
    WhatsAppHistoryAdapter,
    WhatsAppHistoryContractError,
    WhatsAppHistorySnapshotChanged,
    WhatsAppHistoryScanLimitExceeded,
)


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self._body


class FakeHistoryTransport:
    def __init__(self):
        self.calls = []
        self.messages = [
            self._message("m3", 3, "third"),
            self._message("m1", 1, "first"),
            self._message("m2", 2, "second"),
        ]

    @staticmethod
    def _message(message_id, second, text):
        return {
            "source_message_id": message_id,
            "external_thread_key": "chat-a",
            "external_sender_key": "person@c.us",
            "from_me": False,
            "timestamp": f"2026-10-03T00:00:0{second}+00:00",
            "type": "chat",
            "text": text,
            "has_media": False,
            "reply_reference": None,
            "reply_reference_available": False,
        }

    def __call__(self, request, timeout):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "timestamp": request.headers.get("X-attention-timestamp"),
                "signature": request.headers.get("X-attention-signature"),
            }
        )
        parsed = urllib_parse.urlparse(request.full_url)
        if parsed.path == "/internal/history/chats":
            return _Response(
                {
                    "status": "ok",
                    "chats": [
                        {
                            "external_thread_key": "chat-b",
                            "thread_type": "DIRECT",
                            "title": "B",
                        },
                        {
                            "external_thread_key": "chat-a",
                            "thread_type": "GROUP",
                            "title": "A",
                        },
                    ],
                }
            )
        assert parsed.path == "/internal/history/chats/chat-a"
        query = urllib_parse.parse_qs(parsed.query)
        assert query["limit"] in (["1"], ["2"], ["3"])
        assert query["max_scan_messages"] == ["10"]
        cursor = query.get("cursor", [None])[0]
        offset = int(cursor or 0)
        limit = int(query["limit"][0])
        page = sorted(self.messages, key=lambda item: item["timestamp"])[offset:offset + limit]
        next_cursor = str(offset + len(page)) if offset + len(page) < len(self.messages) else None
        return _Response(
            {
                "status": "ok",
                "chat": {
                    "external_thread_key": "chat-a",
                    "thread_type": "DIRECT",
                    "title": "A",
                },
                "pagination_model": "OPAQUE_CURSOR_SNAPSHOT_V1",
                "requested_limit": limit,
                "messages": page,
                "next_cursor": next_cursor,
            }
        )


def _adapter(fake):
    return WhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="h" * 32,
        timeout_seconds=2.5,
        snapshot_limit=3,
        max_scan_messages=10,
        urlopen=fake,
        now=lambda: 1_800_000_000,
    )


def test_history_adapter_lists_chats_deterministically_and_signs_path():
    fake = FakeHistoryTransport()
    adapter = _adapter(fake)

    chats = adapter.list_chats()

    assert [item["external_thread_key"] for item in chats] == [
        "chat-a",
        "chat-b",
    ]
    assert [item["thread_type"] for item in chats] == ["GROUP", "DIRECT"]
    call = fake.calls[-1]
    assert call["timeout"] == 2.5
    assert call["timestamp"] == "1800000000"
    assert call["signature"].startswith("sha256=")


def test_provider_cursor_pages_are_restart_safe_and_oldest_first():
    fake = FakeHistoryTransport()
    adapter = _adapter(fake)

    first = adapter.fetch_messages("chat-a", 2)
    restarted = _adapter(fake)
    second = restarted.fetch_messages(
        "chat-a",
        2,
        first["next_cursor"],
    )

    assert [item["source_message_id"] for item in first["messages"]] == [
        "m1",
        "m2",
    ]
    assert [item["source_message_id"] for item in second["messages"]] == [
        "m3"
    ]
    assert second["next_cursor"] is None


def test_history_hmac_binds_query_and_preserves_metadata():
    fake = FakeHistoryTransport()
    fake.messages[2].update(from_me=True, has_media=True, reply_reference="m0",
                            reply_reference_available=True)
    adapter = _adapter(fake)
    first = adapter.fetch_messages("chat-a", 1)
    parsed = urllib_parse.urlparse(fake.calls[-1]["url"])
    expected = hmac.new(b"h" * 32, f"1800000000.GET.{parsed.path}?{parsed.query}".encode(), hashlib.sha256).hexdigest()
    assert fake.calls[-1]["signature"] == f"sha256={expected}"
    assert first["messages"][0]["source_message_id"] == "m1"
    assert first["messages"][0]["external_sender_key"] == "person@c.us"
    assert first["messages"][0]["from_me"] is False
    assert first["messages"][0]["metadata"]["has_media"] is False
    second = adapter.fetch_messages("chat-a", 1, first["next_cursor"])
    assert second["messages"][0]["from_me"] is True
    assert second["messages"][0]["metadata"] == {
        "provider_type": "chat", "has_media": True, "reply_reference": "m0",
        "reply_reference_available": True,
    }


@pytest.mark.parametrize("status,error", [
    (401, WhatsAppHistoryContractError), (404, WhatsAppHistoryContractError),
    (409, WhatsAppHistorySnapshotChanged), (413, WhatsAppHistoryScanLimitExceeded),
])
def test_history_http_errors_are_bounded(status, error):
    def failing(request, timeout):
        raise HTTPError(request.full_url, status, "synthetic", {}, None)
    with pytest.raises(error) as raised:
        _adapter(failing).fetch_messages("chat-a", 1)
    assert "chat-a" not in str(raised.value)


def test_history_timeout_and_oversized_response_fail_closed():
    def timeout(request, timeout):
        raise URLError("synthetic")
    with pytest.raises(Exception):
        _adapter(timeout).fetch_messages("chat-a", 1)
    class Oversized:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size): return b"x" * size
    with pytest.raises(WhatsAppHistoryContractError):
        _adapter(lambda request, timeout: Oversized()).fetch_messages("chat-a", 1)


def test_history_adapter_rejects_page_larger_than_snapshot():
    fake = FakeHistoryTransport()
    adapter = _adapter(fake)

    with pytest.raises(WhatsAppHistoryContractError):
        adapter.fetch_messages("chat-a", 4)
