"""Read-only, bounded adapter for local whatsapp-web.js history."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


class WhatsAppHistoryError(RuntimeError):
    code = "WHATSAPP_HISTORY_UNAVAILABLE"


class WhatsAppHistoryConfigurationError(WhatsAppHistoryError):
    code = "WHATSAPP_HISTORY_CONFIGURATION_INVALID"


class WhatsAppHistoryContractError(WhatsAppHistoryError):
    code = "WHATSAPP_HISTORY_CONTRACT_INVALID"


class WhatsAppHistorySnapshotChanged(WhatsAppHistoryError):
    code = "WHATSAPP_HISTORY_SNAPSHOT_CHANGED"


class WhatsAppHistoryScanLimitExceeded(WhatsAppHistoryError):
    code = "WHATSAPP_HISTORY_SCAN_LIMIT_EXCEEDED"


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise WhatsAppHistoryContractError()
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WhatsAppHistoryContractError() from exc
    if stamp.tzinfo is None:
        raise WhatsAppHistoryContractError()
    return stamp.astimezone(UTC)


class WhatsAppHistoryAdapter:
    """Bounded, restart-safe adapter over the local transport history surface."""

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        base_url: str,
        hmac_secret: str | None,
        timeout_seconds: float = 5.0,
        snapshot_limit: int = 100,
        max_scan_messages: int = 1000,
        urlopen=None,
        now=None,
    ):
        parsed = urllib_parse.urlparse(base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise WhatsAppHistoryConfigurationError()
        if (timeout_seconds <= 0 or not 1 <= snapshot_limit <= 100
                or not snapshot_limit <= max_scan_messages <= 10000):
            raise WhatsAppHistoryConfigurationError()
        self.base_url = base_url.rstrip("/")
        self.hmac_secret = hmac_secret or ""
        self.timeout_seconds = timeout_seconds
        self.snapshot_limit = snapshot_limit
        self.max_scan_messages = max_scan_messages
        self._urlopen = urlopen or urllib_request.urlopen
        self._now = now or time.time

    def _headers(self, path: str) -> dict[str, str]:
        if not self.hmac_secret:
            raise WhatsAppHistoryConfigurationError()
        timestamp = str(int(self._now()))
        signature = hmac.new(
            self.hmac_secret.encode("utf-8"),
            f"{timestamp}.GET.{path}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Accept": "application/json",
            "X-Attention-Timestamp": timestamp,
            "X-Attention-Signature": f"sha256={signature}",
        }

    def _get_json(self, url: str) -> dict[str, Any]:
        parsed = urllib_parse.urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = urllib_request.Request(
            url,
            method="GET",
            headers=self._headers(path),
        )
        try:
            with self._urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
        except urllib_error.HTTPError as exc:
            if exc.code == 409:
                raise WhatsAppHistorySnapshotChanged() from None
            if exc.code == 413:
                raise WhatsAppHistoryScanLimitExceeded() from None
            if exc.code in {400, 401, 403, 404}:
                raise WhatsAppHistoryContractError() from None
            raise WhatsAppHistoryError() from None
        except (OSError, TimeoutError, urllib_error.URLError):
            raise WhatsAppHistoryError() from None
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise WhatsAppHistoryContractError()
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WhatsAppHistoryContractError() from exc
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise WhatsAppHistoryContractError()
        return value

    @staticmethod
    def _normalize_chat(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WhatsAppHistoryContractError()
        key = value.get("external_thread_key")
        thread_type = value.get("thread_type")
        title = value.get("title")
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 240
            or thread_type not in {"DIRECT", "GROUP"}
            or (title is not None and not isinstance(title, str))
        ):
            raise WhatsAppHistoryContractError()
        return {
            "external_thread_key": key,
            "thread_type": thread_type,
            "title": title[:500] if title else None,
        }

    def list_chats(self) -> list[dict[str, Any]]:
        payload = self._get_json(self.base_url)
        chats = payload.get("chats")
        if not isinstance(chats, list):
            raise WhatsAppHistoryContractError()
        normalized = [self._normalize_chat(item) for item in chats]
        keys = [item["external_thread_key"] for item in normalized]
        if len(keys) != len(set(keys)):
            raise WhatsAppHistoryContractError()
        return sorted(normalized, key=lambda item: item["external_thread_key"])

    @staticmethod
    def _normalize_message(value: object, chat_key: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise WhatsAppHistoryContractError()
        message_id = value.get("source_message_id")
        thread_key = value.get("external_thread_key")
        sender = value.get("external_sender_key")
        text = value.get("text")
        provider_type = value.get("type")
        from_me = value.get("from_me")
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 512
            or thread_key != chat_key
            or (sender is not None and not isinstance(sender, str))
            or not isinstance(from_me, bool)
            or not isinstance(provider_type, str)
            or (text is not None and not isinstance(text, str))
        ):
            raise WhatsAppHistoryContractError()
        sent_at = _parse_timestamp(value.get("timestamp"))
        return {
            "source_message_id": message_id,
            "external_sender_key": sender,
            "sender_display_name": None,
            "sent_at": sent_at,
            "text": text,
            "type": "TEXT" if text is not None else "UNSUPPORTED",
            "from_me": from_me,
            "metadata": {
                "provider_type": provider_type,
                "has_media": bool(value.get("has_media", False)),
                "reply_reference": value.get("reply_reference"),
                "reply_reference_available": bool(
                    value.get("reply_reference_available", False)
                ),
            },
        }

    def fetch_messages(
        self,
        chat_key: str,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(chat_key, str)
            or not chat_key
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > self.snapshot_limit
        ):
            raise WhatsAppHistoryContractError()

        if cursor is not None and (not isinstance(cursor, str) or not cursor
                                   or len(cursor) > 2048):
            raise WhatsAppHistoryContractError()
        quoted = urllib_parse.quote(chat_key, safe="")
        query = {"limit": limit, "max_scan_messages": self.max_scan_messages}
        if cursor is not None:
            query["cursor"] = cursor
        payload = self._get_json(
            f"{self.base_url}/{quoted}?{urllib_parse.urlencode(query)}"
        )
        if (payload.get("pagination_model") != "OPAQUE_CURSOR_SNAPSHOT_V1"
                or payload.get("requested_limit") != limit):
            raise WhatsAppHistoryContractError()
        chat = self._normalize_chat(payload.get("chat"))
        if chat["external_thread_key"] != chat_key:
            raise WhatsAppHistoryContractError()
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or len(raw_messages) > limit:
            raise WhatsAppHistoryContractError()
        page = [self._normalize_message(item, chat_key) for item in raw_messages]
        ids = [item["source_message_id"] for item in page]
        if len(ids) != len(set(ids)):
            raise WhatsAppHistoryContractError()
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str)
                                        or not next_cursor or len(next_cursor) > 2048
                                        or not page):
            raise WhatsAppHistoryContractError()
        return {
            "messages": page,
            "next_cursor": next_cursor,
        }


__all__ = [
    "WhatsAppHistoryAdapter",
    "WhatsAppHistoryConfigurationError",
    "WhatsAppHistoryContractError",
    "WhatsAppHistoryError",
    "WhatsAppHistorySnapshotChanged",
    "WhatsAppHistoryScanLimitExceeded",
]
