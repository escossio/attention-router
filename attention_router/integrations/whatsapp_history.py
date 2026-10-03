"""Read-only local WhatsApp historical adapter for Semantic Bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import time
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from attention_router.integrations.http_transport import urlopen_without_redirects


class WhatsAppHistoryError(RuntimeError):
    pass


class LocalWhatsAppHistoryAdapter:
    """Translate the local transport history wire shape into HistoryAdapter."""

    def __init__(
        self,
        *,
        base_url: str,
        hmac_secret: str,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        opener=None,
        clock=None,
    ) -> None:
        parsed = urllib_parse.urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WHATSAPP_HISTORY_URL_INVALID")
        if not isinstance(hmac_secret, str) or not hmac_secret:
            raise ValueError("WHATSAPP_HISTORY_HMAC_SECRET_REQUIRED")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("WHATSAPP_HISTORY_TIMEOUT_INVALID")
        if not 1024 <= max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("WHATSAPP_HISTORY_RESPONSE_LIMIT_INVALID")

        self._base_url = base_url.rstrip("/")
        self._secret = hmac_secret.encode("utf-8")
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._opener = opener or urlopen_without_redirects
        self._clock = clock or time.time

    def _headers(self, request_target: str) -> dict[str, str]:
        timestamp = str(int(self._clock()))
        signature = hmac.new(
            self._secret,
            f"{timestamp}.GET.{request_target}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Accept": "application/json",
            "X-Attention-Timestamp": timestamp,
            "X-Attention-Signature": f"sha256={signature}",
        }

    def _get_json(
        self,
        path_suffix: str = "",
        *,
        query: list[tuple[str, str | int]] | None = None,
    ) -> dict[str, Any]:
        url = self._base_url + path_suffix
        if query:
            url += "?" + urllib_parse.urlencode(query)
        parsed = urllib_parse.urlsplit(url)
        request_target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = urllib_request.Request(
            url,
            method="GET",
            headers=self._headers(request_target),
        )
        failure = "WHATSAPP_HISTORY_RESPONSE_INVALID"
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
            try:
                status = int(getattr(response, "status", 0))
                if status == 404:
                    failure = "WHATSAPP_HISTORY_CHAT_NOT_FOUND"
                elif status == 409:
                    failure = "WHATSAPP_HISTORY_CURSOR_STALE"
                elif status == 413:
                    failure = "WHATSAPP_HISTORY_SCAN_LIMIT_EXCEEDED"
                elif status == 503:
                    failure = "WHATSAPP_HISTORY_UNAVAILABLE"
                elif status != 200:
                    failure = "WHATSAPP_HISTORY_REJECTED"
                else:
                    body = response.read(self._max_response_bytes + 1)
                    if len(body) > self._max_response_bytes:
                        failure = "WHATSAPP_HISTORY_RESPONSE_TOO_LARGE"
                    else:
                        decoded = json.loads(body.decode("utf-8"))
                        if isinstance(decoded, dict) and decoded.get("status") == "ok":
                            return decoded
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib_error.HTTPError as exc:
            if exc.code == 404:
                failure = "WHATSAPP_HISTORY_CHAT_NOT_FOUND"
            elif exc.code == 409:
                failure = "WHATSAPP_HISTORY_CURSOR_STALE"
            elif exc.code == 413:
                failure = "WHATSAPP_HISTORY_SCAN_LIMIT_EXCEEDED"
            elif exc.code == 503:
                failure = "WHATSAPP_HISTORY_UNAVAILABLE"
            else:
                failure = "WHATSAPP_HISTORY_REJECTED"
        except (OSError, TimeoutError):
            failure = "WHATSAPP_HISTORY_UNAVAILABLE"
        except Exception:
            pass
        raise WhatsAppHistoryError(failure)

    @staticmethod
    def _chat(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_CHAT_INVALID")
        key = payload.get("external_thread_key")
        thread_type = payload.get("thread_type")
        title = payload.get("title")
        if not isinstance(key, str) or not key:
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_CHAT_INVALID")
        if thread_type not in {"DIRECT", "GROUP"}:
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_CHAT_INVALID")
        if title is not None and not isinstance(title, str):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_CHAT_INVALID")
        return {
            "external_thread_key": key,
            "thread_type": thread_type,
            "title": title,
        }

    @staticmethod
    def _message(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        message_id = payload.get("source_message_id")
        timestamp = payload.get("timestamp")
        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        if not isinstance(timestamp, str) or not timestamp:
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        try:
            sent_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WhatsAppHistoryError(
                "WHATSAPP_HISTORY_MESSAGE_INVALID"
            ) from exc
        if sent_at.tzinfo is None:
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        sender = payload.get("external_sender_key")
        text = payload.get("text")
        if sender is not None and not isinstance(sender, str):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        if text is not None and not isinstance(text, str):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_MESSAGE_INVALID")
        raw_type = payload.get("type")
        message_type = (
            "TEXT"
            if str(raw_type or "").casefold() in {"chat", "text"}
            else str(raw_type or "UNSUPPORTED").upper()
        )
        return {
            "source_message_id": message_id,
            "external_sender_key": sender,
            "sender_display_name": None,
            "sent_at": sent_at.astimezone(UTC),
            "text": text,
            "type": message_type,
            "from_me": bool(payload.get("from_me", False)),
            "metadata": {
                "external_thread_key": payload.get("external_thread_key"),
                "has_media": bool(payload.get("has_media", False)),
                "reply_reference": payload.get("reply_reference"),
                "reply_reference_available": bool(
                    payload.get("reply_reference_available", False)
                ),
            },
        }

    def list_chats(self) -> list[dict[str, Any]]:
        payload = self._get_json()
        chats = payload.get("chats")
        if not isinstance(chats, list):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_RESPONSE_INVALID")
        return [self._chat(chat) for chat in chats]

    def fetch_messages(
        self,
        chat_key: str,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(chat_key, str) or not chat_key:
            raise ValueError("WHATSAPP_HISTORY_CHAT_KEY_REQUIRED")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("WHATSAPP_HISTORY_LIMIT_OUT_OF_RANGE")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or len(cursor) > 4096
        ):
            raise ValueError("WHATSAPP_HISTORY_CURSOR_INVALID")

        query: list[tuple[str, str | int]] = [("limit", limit)]
        if cursor is not None:
            query.append(("cursor", cursor))
        payload = self._get_json(
            "/" + urllib_parse.quote(chat_key, safe=""),
            query=query,
        )
        messages = payload.get("messages")
        next_cursor = payload.get("next_cursor")
        if not isinstance(messages, list):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_RESPONSE_INVALID")
        if next_cursor is not None and (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > 4096
        ):
            raise WhatsAppHistoryError("WHATSAPP_HISTORY_RESPONSE_INVALID")
        return {
            "messages": [self._message(item) for item in messages],
            "next_cursor": next_cursor,
        }


__all__ = ["LocalWhatsAppHistoryAdapter", "WhatsAppHistoryError"]
