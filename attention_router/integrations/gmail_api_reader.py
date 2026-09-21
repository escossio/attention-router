from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
import json
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from attention_router.integrations.gmail_connector import (
    GmailConnectorError,
    GmailMessage,
    GmailReader,
)
from attention_router.integrations.http_transport import urlopen_without_redirects


GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_METADATA_HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "Date")


class GmailAccessTokenProvider(Protocol):
    def access_token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StaticGmailAccessTokenProvider:
    """Opaque token source. The token lifecycle remains external."""

    token: str = field(repr=False)

    def access_token(self) -> str:
        if not isinstance(self.token, str) or not self.token.strip():
            raise GmailConnectorError("GMAIL_ACCESS_TOKEN_UNAVAILABLE")
        return self.token.strip()


def history_id(value: object) -> str:
    if (
        not isinstance(value, str) or not value.isascii() or not value.isdecimal()
        or not 1 <= len(value) <= 20 or not 0 < int(value) <= 2**64 - 1
    ):
        raise GmailConnectorError("GMAIL_API_RESPONSE_INVALID")
    return value


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
        self._opener = opener or urlopen_without_redirects

    def _get_json(
        self,
        path: str,
        *,
        query: dict[str, str | int | tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        url = GMAIL_API_BASE + path
        if query:
            url += "?" + urllib_parse.urlencode(query, doseq=True)
        token = self._token_provider.access_token()
        request = urllib_request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        failure = "GMAIL_API_RESPONSE_INVALID"
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
            try:
                if response.status == 404 and path == "/history":
                    failure = "GMAIL_PRODUCT_HISTORY_STALE"
                elif response.status in {401, 403}:
                    failure = "GMAIL_API_UNAUTHENTICATED"
                elif response.status == 429 or 500 <= response.status <= 599:
                    failure = "GMAIL_API_UNAVAILABLE"
                elif response.status != 200:
                    failure = "GMAIL_API_REJECTED"
                else:
                    body = response.read(1024 * 1024 + 1)
                    if len(body) <= 1024 * 1024:
                        decoded = json.loads(body.decode("utf-8"))
                        if isinstance(decoded, dict):
                            return decoded
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib_error.HTTPError as exc:
            if exc.code == 404 and path == "/history":
                failure = "GMAIL_PRODUCT_HISTORY_STALE"
            elif exc.code in {401, 403}:
                failure = "GMAIL_API_UNAUTHENTICATED"
            elif exc.code == 429 or 500 <= exc.code <= 599:
                failure = "GMAIL_API_UNAVAILABLE"
            else:
                failure = "GMAIL_API_REJECTED"
        except (OSError, TimeoutError):
            failure = "GMAIL_API_UNAVAILABLE"
        except Exception:
            pass
        # Do not retain untrusted response bodies/URLs in exception chains.
        raise GmailConnectorError(failure)

    def current_history_id(self) -> str:
        return history_id(self._get_json("/profile", query={"fields": "historyId"}).get(
            "historyId"
        ))

    def history_page(self, start: str, page_token: str | None = None) -> dict:
        query = {
            "startHistoryId": history_id(start),
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
            "maxResults": 100,
            "fields": "history(id,messagesAdded(message(id,labelIds))),historyId,nextPageToken",
        }
        if page_token is not None:
            query["pageToken"] = page_token
        return self._get_json("/history", query=query)

    def search_message_ids(
        self,
        *,
        query: str,
        max_results: int,
    ) -> tuple[str, ...]:
        if not isinstance(query, str):
            raise TypeError("query must be str")
        if query.strip():
            raise GmailConnectorError("GMAIL_METADATA_QUERY_FORBIDDEN")
        if not 1 <= max_results <= 100:
            raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")
        payload = self._get_json(
            "/messages",
            query={
                "labelIds": "INBOX",
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
            query={
                "format": "metadata",
                "metadataHeaders": GMAIL_METADATA_HEADERS,
            },
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
        address.strip()
        for _display, address in getaddresses([raw])
        if address.strip()
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

    return GmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        to=_recipient_values(headers.get("to")),
        cc=_recipient_values(headers.get("cc")),
        bcc=_recipient_values(headers.get("bcc")),
        subject=headers.get("subject", ""),
        body="",
        email_ts=_message_timestamp(payload, headers),
        attachments=(),
        body_observed=False,
        attachments_observed=False,
    )


__all__ = [
    "GMAIL_API_BASE",
    "GMAIL_METADATA_HEADERS",
    "GmailAccessTokenProvider",
    "GmailApiReader",
    "StaticGmailAccessTokenProvider",
]
