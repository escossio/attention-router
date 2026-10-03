"""Read-only production HistoryAdapter for local whatsapp-web.js history.

V0 intentionally treats the provider as LIMIT_ONLY. A per-chat snapshot is
bounded and fingerprinted. Resuming a partial page re-fetches the same bounded
window and fails closed if it changed instead of inventing cursor semantics.
"""

from __future__ import annotations

import base64
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


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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

    CURSOR_PREFIX = "whv0."
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        base_url: str,
        hmac_secret: str | None,
        timeout_seconds: float = 5.0,
        snapshot_limit: int = 100,
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
        if timeout_seconds <= 0 or not 1 <= snapshot_limit <= 100:
            raise WhatsAppHistoryConfigurationError()
        self.base_url = base_url.rstrip("/")
        self.hmac_secret = hmac_secret or ""
        self.timeout_seconds = timeout_seconds
        self.snapshot_limit = snapshot_limit
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
        path = urllib_parse.urlparse(url).path
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
            if exc.code in {401, 403, 404}:
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

    @classmethod
    def _encode_cursor(
        cls,
        *,
        chat_key: str,
        snapshot_limit: int,
        fingerprint: str,
        offset: int,
    ) -> str:
        raw = _canonical_json(
            {
                "v": 1,
                "chat_key": chat_key,
                "snapshot_limit": snapshot_limit,
                "fingerprint": fingerprint,
                "offset": offset,
            }
        )
        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return cls.CURSOR_PREFIX + token

    @classmethod
    def _decode_cursor(cls, value: str) -> dict[str, Any]:
        if not isinstance(value, str) or not value.startswith(cls.CURSOR_PREFIX):
            raise WhatsAppHistoryContractError()
        token = value[len(cls.CURSOR_PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            decoded = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise WhatsAppHistoryContractError() from exc
        if not isinstance(decoded, dict) or decoded.get("v") != 1:
            raise WhatsAppHistoryContractError()
        return decoded

    def _snapshot(self, chat_key: str) -> tuple[list[dict[str, Any]], str]:
        quoted = urllib_parse.quote(chat_key, safe="")
        url = f"{self.base_url}/{quoted}?limit={self.snapshot_limit}"
        payload = self._get_json(url)
        if (
            payload.get("pagination_model") != "LIMIT_ONLY"
            or payload.get("requested_limit") != self.snapshot_limit
        ):
            raise WhatsAppHistoryContractError()
        chat = self._normalize_chat(payload.get("chat"))
        if chat["external_thread_key"] != chat_key:
            raise WhatsAppHistoryContractError()
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or len(raw_messages) > self.snapshot_limit:
            raise WhatsAppHistoryContractError()
        messages = [
            self._normalize_message(item, chat_key)
            for item in raw_messages
        ]
        ids = [item["source_message_id"] for item in messages]
        if len(ids) != len(set(ids)):
            raise WhatsAppHistoryContractError()
        messages.sort(
            key=lambda item: (
                item["sent_at"],
                item["source_message_id"],
            )
        )
        fingerprint_payload = [
            {
                **item,
                "sent_at": item["sent_at"].isoformat(),
            }
            for item in messages
        ]
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload)
        ).hexdigest()
        return messages, fingerprint

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

        snapshot, fingerprint = self._snapshot(chat_key)
        text_messages = [
            item for item in snapshot
            if item["text"] is not None
        ]

        offset = 0
        if cursor is not None:
            decoded = self._decode_cursor(cursor)
            if (
                decoded.get("chat_key") != chat_key
                or decoded.get("snapshot_limit") != self.snapshot_limit
                or decoded.get("fingerprint") != fingerprint
                or isinstance(decoded.get("offset"), bool)
                or not isinstance(decoded.get("offset"), int)
                or decoded["offset"] < 0
                or decoded["offset"] > len(text_messages)
            ):
                raise WhatsAppHistorySnapshotChanged()
            offset = decoded["offset"]

        page = text_messages[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            self._encode_cursor(
                chat_key=chat_key,
                snapshot_limit=self.snapshot_limit,
                fingerprint=fingerprint,
                offset=next_offset,
            )
            if next_offset < len(text_messages)
            else None
        )
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
]
