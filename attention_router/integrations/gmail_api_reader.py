from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
from typing import Any, Protocol
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorError,
    GmailMessage,
    GmailReader,
)


GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailAccessTokenProvider(Protocol):
    def access_token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StaticGmailAccessTokenProvider:
    """Canary-oriented token source. The token lifecycle remains external."""

    token: str

    def access_token(self) -> str:
        if not isinstance(self.token, str) or not self.token.strip():
            raise GmailConnectorError("GMAIL_ACCESS_TOKEN_UNAVAILABLE")
        return self.token.strip()


class GmailApiReader(GmailReader):
    """Read-only Gmail REST reader with no provider state mutation."""

    def __init__(
        self,
        *,
        token_provider: GmailAccessTokenProvider,
        timeout_seconds: float = 10.0,
        opener=None,
    ):
        self._token_provider = token_provider
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urllib_request.urlopen

    def _get_json(
        self,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        url = GMAIL_API_BASE + path
        if query:
            url += "?" + urllib_parse.urlencode(query)
        token = self._token_provider.access_token()
        request = urllib_request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            response = self._opener(
                request,
                timeout=self._timeout_seconds,
            )
            status = int(response.status)
            body = response.read()
        except Exception as exc:
            # Do not leak provider response bodies, tokens, or request URLs.
            raise GmailConnectorError("GMAIL_API_UNAVAILABLE") from exc

        if status != 200:
            raise GmailConnectorError("GMAIL_API_REJECTED")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID") from exc
        if not isinstance(decoded, dict):
            raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")
        return decoded

    def search_message_ids(
        self,
        *,
        query: str,
        max_results: int,
    ) -> tuple[str, ...]:
        if not isinstance(query, str):
            raise TypeError("query must be str")
        if not 1 <= max_results <= 100:
            raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")
        payload = self._get_json(
            "/messages",
            query={
                "q": query,
                "maxResults": max_results,
                "includeSpamTrash": "false",
            },
        )
        messages = payload.get("messages", [])
        if messages is None:
            return ()
        if not isinstance(messages, list):
            raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")

        result: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")
            message_id = item.get("id")
            if not isinstance(message_id, str) or not message_id:
                raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")
            result.append(message_id)
        return tuple(result)

    def read_message(self, message_id: str) -> GmailMessage:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("GMAIL_MESSAGE_ID_REQUIRED")
        payload = self._get_json(
            f"/messages/{urllib_parse.quote(message_id, safe='')}",
            query={"format": "full"},
        )
        return _gmail_api_payload_to_message(payload)


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    part = payload.get("payload")
    if not isinstance(part, dict):
        raise GmailConnectorError("GMAIL_API_MESSAGE_PAYLOAD_INVALID")
    headers = part.get("headers")
    if not isinstance(headers, list):
        raise GmailConnectorError("GMAIL_API_MESSAGE_PAYLOAD_INVALID")

    result: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.casefold() not in result
        ):
            result[name.casefold()] = value
    return result


def _recipient_values(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(
        item.strip()
        for item in raw.split(",")
        if item.strip()
    )


def _message_timestamp(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> str:
    internal_date = payload.get("internalDate")
    if isinstance(internal_date, str) and internal_date.isdigit():
        stamp = datetime.fromtimestamp(
            int(internal_date) / 1000,
            tz=UTC,
        )
        return stamp.isoformat()

    raw_date = headers.get("date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
    raise GmailConnectorError("GMAIL_API_MESSAGE_TIMESTAMP_INVALID")


def _attachment_summaries(
    root: dict[str, Any],
) -> tuple[GmailAttachmentSummary, ...]:
    found: list[GmailAttachmentSummary] = []

    def visit(part: Any) -> None:
        if not isinstance(part, dict):
            return
        body = part.get("body")
        filename = part.get("filename")
        mime_type = part.get("mimeType")
        if isinstance(body, dict):
            attachment_id = body.get("attachmentId")
            size = body.get("size")
            if (
                isinstance(attachment_id, str)
                and attachment_id
            ) or (
                isinstance(filename, str)
                and filename.strip()
            ):
                found.append(
                    GmailAttachmentSummary(
                        attachment_id=(
                            attachment_id
                            if isinstance(attachment_id, str)
                            and attachment_id
                            else None
                        ),
                        filename=(
                            filename.strip()
                            if isinstance(filename, str)
                            and filename.strip()
                            else "unnamed-attachment"
                        ),
                        mime_type=(
                            mime_type
                            if isinstance(mime_type, str)
                            and mime_type
                            else "application/octet-stream"
                        ),
                        size_bytes=(
                            int(size)
                            if isinstance(size, int) and size >= 0
                            else None
                        ),
                    )
                )
        children = part.get("parts")
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(root)
    return tuple(found)


def _gmail_api_payload_to_message(
    payload: dict[str, Any],
) -> GmailMessage:
    message_id = payload.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise GmailConnectorError("GMAIL_API_MESSAGE_ID_INVALID")

    thread_id = payload.get("threadId")
    if thread_id is not None and not isinstance(thread_id, str):
        raise GmailConnectorError("GMAIL_API_THREAD_ID_INVALID")

    headers = _header_map(payload)
    sender = headers.get("from")
    if not sender:
        raise GmailConnectorError("GMAIL_API_SENDER_MISSING")

    root = payload["payload"]
    snippet = payload.get("snippet")
    if snippet is not None and not isinstance(snippet, str):
        raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")

    return GmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        to=_recipient_values(headers.get("to")),
        cc=_recipient_values(headers.get("cc")),
        bcc=_recipient_values(headers.get("bcc")),
        subject=headers.get("subject", ""),
        # Snippet is sufficient for bounded body-presence detection. The
        # connector never serializes it into Integration Contract V1.
        body=snippet or "",
        email_ts=_message_timestamp(payload, headers),
        attachments=_attachment_summaries(root),
    )


__all__ = [
    "GMAIL_API_BASE",
    "GmailAccessTokenProvider",
    "GmailApiReader",
    "StaticGmailAccessTokenProvider",
]
