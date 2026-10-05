from __future__ import annotations

from dataclasses import dataclass, field, replace
import base64
import binascii
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
import json
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
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
        max_response_bytes: int = 1024 * 1024,
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
                    body = response.read(max_response_bytes + 1)
                    if len(body) <= max_response_bytes:
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

    def read_message_with_body(
        self,
        message_id: str,
        *,
        max_bytes: int,
        max_mime_depth: int,
    ) -> GmailMessage:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("GMAIL_MESSAGE_ID_REQUIRED")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024:
            raise ValueError("GMAIL_BODY_MAX_BYTES_OUT_OF_RANGE")
        if type(max_mime_depth) is not int or not 1 <= max_mime_depth <= 32:
            raise ValueError("GMAIL_BODY_MIME_DEPTH_OUT_OF_RANGE")
        encoded_limit = 4 * ((max_bytes + 2) // 3) + 256 * 1024
        payload = self._get_json(
            f"/messages/{urllib_parse.quote(message_id, safe='')}",
            query={
                "format": "full",
                "fields": (
                    "id,threadId,internalDate,"
                    f"payload({_body_part_projection(max_mime_depth)})"
                ),
            },
            max_response_bytes=encoded_limit,
        )
        message = _gmail_api_payload_to_message(payload)
        body = _message_text_body(
            payload,
            max_bytes=max_bytes,
            max_mime_depth=max_mime_depth,
            read_external=lambda attachment_id, expected_size, remaining: self.read_attachment(
                message_id,
                attachment_id,
                expected_size=expected_size,
                max_bytes=remaining,
            ),
        )
        return replace(message, body=body, body_observed=True)


    def read_message_with_attachments(
        self,
        message_id: str,
        *,
        max_attachments: int,
        max_mime_depth: int,
    ) -> GmailMessage:
        if type(max_attachments) is not int or not 1 <= max_attachments <= 64:
            raise ValueError("GMAIL_ATTACHMENT_MAX_COUNT_OUT_OF_RANGE")
        if type(max_mime_depth) is not int or not 1 <= max_mime_depth <= 32:
            raise ValueError("GMAIL_ATTACHMENT_MIME_DEPTH_OUT_OF_RANGE")
        message = self.read_message(message_id)
        structure = self._get_json(
            f"/messages/{urllib_parse.quote(message_id, safe='')}",
            query={
                "format": "full",
                "fields": f"payload({_mime_part_projection(max_mime_depth)})",
            },
        )
        attachments = _attachment_summaries(
            structure,
            max_attachments=max_attachments,
            max_mime_depth=max_mime_depth,
        )
        return replace(
            message,
            attachments=attachments,
            attachments_observed=True,
        )

    def read_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        expected_size: int,
        max_bytes: int,
    ) -> bytes:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("GMAIL_MESSAGE_ID_REQUIRED")
        if (
            not isinstance(attachment_id, str)
            or not 1 <= len(attachment_id) <= 2048
            or attachment_id.isspace()
        ):
            raise ValueError("GMAIL_ATTACHMENT_ID_REQUIRED")
        if (
            type(expected_size) is not int
            or expected_size < 0
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 1024 * 1024 * 1024
            or expected_size > max_bytes
        ):
            raise GmailConnectorError("GMAIL_ATTACHMENT_SIZE_INVALID")

        encoded_limit = 4 * ((max_bytes + 2) // 3) + 8
        payload = self._get_json(
            (
                f"/messages/{urllib_parse.quote(message_id, safe='')}"
                f"/attachments/{urllib_parse.quote(attachment_id, safe='')}"
            ),
            query={"fields": "size,data"},
            max_response_bytes=encoded_limit + 4096,
        )
        reported_size = payload.get("size")
        encoded = payload.get("data")
        if (
            type(reported_size) is not int
            or reported_size != expected_size
            or not isinstance(encoded, str)
            or len(encoded) > encoded_limit
        ):
            raise GmailConnectorError("GMAIL_ATTACHMENT_RESPONSE_INVALID")
        try:
            raw = encoded.encode("ascii")
            padding = b"=" * ((-len(raw)) % 4)
            decoded = base64.b64decode(
                raw + padding,
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError):
            raise GmailConnectorError("GMAIL_ATTACHMENT_RESPONSE_INVALID") from None
        if len(decoded) != expected_size:
            raise GmailConnectorError("GMAIL_ATTACHMENT_SIZE_MISMATCH")
        return decoded


def _body_part_projection(depth: int) -> str:
    fields = "mimeType,filename,headers(name,value),body(attachmentId,size,data)"
    if depth > 1:
        fields += f",parts({_body_part_projection(depth - 1)})"
    return fields


def _part_headers(part: dict[str, Any]) -> dict[str, str]:
    raw = part.get("headers", [])
    if not isinstance(raw, list):
        raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result.setdefault(name.casefold(), value)
    return result


def _decode_body_data(encoded: str, *, expected_size: int, max_bytes: int) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 4 * ((max_bytes + 2) // 3) + 8:
        raise GmailConnectorError("GMAIL_BODY_RESPONSE_INVALID")
    try:
        raw = encoded.encode("ascii")
        padding = b"=" * ((-len(raw)) % 4)
        decoded = base64.b64decode(raw + padding, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise GmailConnectorError("GMAIL_BODY_RESPONSE_INVALID") from None
    if len(decoded) != expected_size or len(decoded) > max_bytes:
        raise GmailConnectorError("GMAIL_BODY_SIZE_INVALID")
    return decoded


def _decode_text_part(data: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "text/plain; charset=utf-8")
    message = Message()
    message["content-type"] = content_type
    charset = message.get_content_charset() or "utf-8"
    try:
        text = data.decode(charset)
    except (LookupError, UnicodeDecodeError):
        raise GmailConnectorError("GMAIL_BODY_ENCODING_UNSUPPORTED") from None
    if "\x00" in text:
        raise GmailConnectorError("GMAIL_BODY_TEXT_INVALID")
    return text


_HTML_SUPPRESSED_TAGS = frozenset({
    "head",
    "iframe",
    "math",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
})
_HTML_BLOCK_TAGS = frozenset({
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
})


class _InertHtmlTextExtractor(HTMLParser):
    """Extract visible text without rendering markup or inspecting attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppressed: list[str] = []
        self._events = 0

    def _bounded_event(self) -> None:
        self._events += 1
        if self._events > 65536:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_TOO_LARGE")

    def _separator(self) -> None:
        if not self._suppressed:
            self._chunks.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        self._bounded_event()
        normalized = tag.casefold()
        if self._suppressed:
            if normalized in _HTML_SUPPRESSED_TAGS:
                self._suppressed.append(normalized)
            return
        if normalized in _HTML_SUPPRESSED_TAGS:
            self._suppressed.append(normalized)
            return
        if normalized in _HTML_BLOCK_TAGS:
            self._separator()

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._bounded_event()
        normalized = tag.casefold()
        if not self._suppressed and normalized in _HTML_BLOCK_TAGS:
            self._separator()

    def handle_endtag(self, tag: str) -> None:
        self._bounded_event()
        normalized = tag.casefold()
        if self._suppressed:
            if normalized == self._suppressed[-1]:
                self._suppressed.pop()
            elif normalized in self._suppressed:
                raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
            return
        if normalized in _HTML_BLOCK_TAGS:
            self._separator()

    def handle_data(self, data: str) -> None:
        self._bounded_event()
        if not self._suppressed:
            self._chunks.append(data)

    def result(self) -> str:
        if self._suppressed:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        lines = []
        for line in "".join(self._chunks).splitlines():
            normalized = " ".join(line.split())
            if normalized:
                lines.append(normalized)
        return "\n".join(lines).strip()


def _html_to_inert_text(value: str) -> str:
    parser = _InertHtmlTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except GmailConnectorError:
        raise
    except Exception:
        raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID") from None
    return parser.result()


def _decode_body_candidates(
    candidates: list[tuple[dict[str, Any], dict[str, str]]],
    *,
    max_bytes: int,
    read_external,
    html_fallback: bool = False,
) -> str:
    parts: list[str] = []
    consumed = 0
    for body, headers in candidates:
        size = body.get("size", 0)
        if not size:
            continue
        remaining = max_bytes - consumed
        if size > remaining or remaining <= 0:
            raise GmailConnectorError("GMAIL_BODY_SIZE_EXCEEDED")
        data = body.get("data")
        attachment_id = body.get("attachmentId")
        if data is not None and attachment_id is not None:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        if data is not None:
            decoded = _decode_body_data(
                data,
                expected_size=size,
                max_bytes=remaining,
            )
        elif attachment_id is not None:
            if (
                not isinstance(attachment_id, str)
                or not 1 <= len(attachment_id) <= 2048
            ):
                raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
            decoded = read_external(attachment_id, size, remaining)
            if len(decoded) != size:
                raise GmailConnectorError("GMAIL_BODY_SIZE_INVALID")
        else:
            raise GmailConnectorError("GMAIL_BODY_CONTENT_MISSING")
        consumed += len(decoded)
        value = _decode_text_part(decoded, headers)
        if html_fallback:
            value = _html_to_inert_text(value)
        if value.strip():
            parts.append(value.strip())
    return "\n".join(parts).strip()


def _message_text_body(
    payload: dict[str, Any],
    *,
    max_bytes: int,
    max_mime_depth: int,
    read_external,
) -> str:
    root = payload.get("payload")
    if not isinstance(root, dict):
        raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
    plain_candidates: list[tuple[dict[str, Any], dict[str, str]]] = []
    html_candidates: list[tuple[dict[str, Any], dict[str, str]]] = []
    nodes = 0

    def walk(part: dict[str, Any], depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 1024:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_TOO_LARGE")
        mime_type = part.get("mimeType")
        if not isinstance(mime_type, str) or not 1 <= len(mime_type) <= 160:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        filename = part.get("filename", "")
        if not isinstance(filename, str) or len(filename) > 512:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        headers = _part_headers(part)
        disposition = headers.get("content-disposition", "").casefold()
        body = part.get("body") or {}
        if not isinstance(body, dict):
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
        size = body.get("size", 0)
        if type(size) is not int or size < 0:
            raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")

        if not filename and "attachment" not in disposition:
            normalized_mime = mime_type.casefold()
            if normalized_mime == "text/plain":
                plain_candidates.append((body, headers))
            elif normalized_mime == "text/html":
                html_candidates.append((body, headers))

        children = part.get("parts")
        if children is not None:
            if not isinstance(children, list) or len(children) > 512:
                raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
            if depth >= max_mime_depth and children:
                raise GmailConnectorError("GMAIL_BODY_MIME_DEPTH_EXCEEDED")
            for child in children:
                if not isinstance(child, dict):
                    raise GmailConnectorError("GMAIL_BODY_STRUCTURE_INVALID")
                walk(child, depth + 1)
        elif depth >= max_mime_depth and mime_type.casefold().startswith("multipart/"):
            raise GmailConnectorError("GMAIL_BODY_MIME_DEPTH_EXCEEDED")

    walk(root, 1)
    plain = _decode_body_candidates(
        plain_candidates,
        max_bytes=max_bytes,
        read_external=read_external,
    )
    if plain:
        return plain
    return _decode_body_candidates(
        html_candidates,
        max_bytes=max_bytes,
        read_external=read_external,
        html_fallback=True,
    )


def _mime_part_projection(depth: int) -> str:
    fields = "mimeType,filename,body(attachmentId,size)"
    if depth > 1:
        fields += f",parts({_mime_part_projection(depth - 1)})"
    return fields


def _attachment_summaries(
    payload: dict[str, Any],
    *,
    max_attachments: int,
    max_mime_depth: int,
) -> tuple[GmailAttachmentSummary, ...]:
    root = payload.get("payload")
    if not isinstance(root, dict):
        raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")

    attachments: list[GmailAttachmentSummary] = []
    seen_attachment_ids: set[str] = set()
    nodes = 0

    def walk(part: dict[str, Any], depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 1024:
            raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_TOO_LARGE")

        mime_type = part.get("mimeType")
        if not isinstance(mime_type, str) or not 1 <= len(mime_type) <= 160:
            raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
        filename = part.get("filename", "")
        if not isinstance(filename, str) or len(filename) > 512:
            raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
        body = part.get("body")
        if body is None:
            body = {}
        if not isinstance(body, dict) or "data" in body:
            raise GmailConnectorError("GMAIL_MESSAGE_BODY_DATA_FORBIDDEN")

        attachment_id = body.get("attachmentId")
        size = body.get("size", 0)
        if type(size) is not int or size < 0:
            raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
        if attachment_id is not None:
            if (
                not isinstance(attachment_id, str)
                or not 1 <= len(attachment_id) <= 2048
                or attachment_id in seen_attachment_ids
            ):
                raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
            if not filename:
                # Gmail may externalize a large MIME body behind attachmentId.
                # Without a filename V1 cannot prove this is user attachment
                # content rather than message-body content, so fail closed.
                raise GmailConnectorError("GMAIL_ATTACHMENT_AMBIGUOUS_PART")
            seen_attachment_ids.add(attachment_id)
            attachments.append(
                GmailAttachmentSummary(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=size,
                )
            )
            if len(attachments) > max_attachments:
                raise GmailConnectorError("GMAIL_ATTACHMENT_COUNT_EXCEEDED")
        elif filename and size > 0:
            # Reading body.data to recover an inline attachment would also risk
            # observing message-body bytes. V1 fails closed instead.
            raise GmailConnectorError("GMAIL_INLINE_ATTACHMENT_UNSUPPORTED")

        parts = part.get("parts")
        if parts is not None:
            if not isinstance(parts, list) or len(parts) > 512:
                raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
            if depth >= max_mime_depth and parts:
                raise GmailConnectorError("GMAIL_ATTACHMENT_MIME_DEPTH_EXCEEDED")
            for child in parts:
                if not isinstance(child, dict):
                    raise GmailConnectorError("GMAIL_ATTACHMENT_STRUCTURE_INVALID")
                walk(child, depth + 1)
        elif depth >= max_mime_depth and mime_type.casefold().startswith("multipart/"):
            raise GmailConnectorError("GMAIL_ATTACHMENT_MIME_DEPTH_EXCEEDED")

    walk(root, 1)
    return tuple(attachments)


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
