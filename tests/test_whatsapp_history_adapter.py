from __future__ import annotations

import json
from urllib import parse as urllib_parse

import pytest

from attention_router.integrations.whatsapp_history import (
    WhatsAppHistoryAdapter,
    WhatsAppHistoryContractError,
    WhatsAppHistorySnapshotChanged,
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
                            "thread_type": "DIRECT",
                            "title": "A",
                        },
                    ],
                }
            )
        assert parsed.path == "/internal/history/chats/chat-a"
        assert urllib_parse.parse_qs(parsed.query) == {"limit": ["3"]}
        return _Response(
            {
                "status": "ok",
                "chat": {
                    "external_thread_key": "chat-a",
                    "thread_type": "DIRECT",
                    "title": "A",
                },
                "pagination_model": "LIMIT_ONLY",
                "requested_limit": 3,
                "messages": list(self.messages),
            }
        )


def _adapter(fake):
    return WhatsAppHistoryAdapter(
        base_url="http://127.0.0.1:18103/internal/history/chats",
        hmac_secret="h" * 32,
        timeout_seconds=2.5,
        snapshot_limit=3,
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
    call = fake.calls[-1]
    assert call["timeout"] == 2.5
    assert call["timestamp"] == "1800000000"
    assert call["signature"].startswith("sha256=")


def test_limit_only_snapshot_pages_are_restart_safe_and_oldest_first():
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


def test_limit_only_resume_fails_closed_if_bounded_snapshot_changes():
    fake = FakeHistoryTransport()
    adapter = _adapter(fake)
    first = adapter.fetch_messages("chat-a", 1)

    fake.messages[0] = fake._message("m4", 4, "new")
    with pytest.raises(WhatsAppHistorySnapshotChanged):
        adapter.fetch_messages(
            "chat-a",
            1,
            first["next_cursor"],
        )


def test_history_adapter_rejects_page_larger_than_snapshot():
    fake = FakeHistoryTransport()
    adapter = _adapter(fake)

    with pytest.raises(WhatsAppHistoryContractError):
        adapter.fetch_messages("chat-a", 4)
