"""Gmail historical text source for governed Personal Context bootstrap.

This module is intentionally separate from the continuous Gmail ingress reader.
Continuous ingestion keeps its metadata/body-observation boundary unchanged.
Bootstrap body observation is available only to an explicitly authorized
gmail.readonly source path.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from email.utils import parseaddr
from typing import Any
from urllib import parse as urllib_parse

from attention_router.integrations.gmail_api_reader import (
    GmailApiReader,
    _gmail_api_payload_to_message,
)
from attention_router.integrations.gmail_connector import (
    GmailConnectorError,
    GmailMessage,
)


class GmailBootstrapError(RuntimeError):
    code = "GMAIL_BOOTSTRAP_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


class GmailBootstrapContractError(GmailBootstrapError):
    code = "GMAIL_BOOTSTRAP_CONTRACT_INVALID"


class GmailBootstrapSnapshotInvalid(GmailBootstrapError):
    code = "GMAIL_BOOTSTRAP_SNAPSHOT_INVALID"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _headers(part: object) -> dict[str, str]:
    if not isinstance(part, dict):
        raise GmailBootstrapContractError()
    raw = part.get("headers", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 200:
        raise GmailBootstrapContractError()
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise GmailBootstrapContractError()
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise GmailBootstrapContractError()
        key = name.casefold()
        if key not in result:
            result[key] = value
    return result


def _content_charset(part: dict[str, Any]) -> str:
    raw = _headers(part).get("content-type", "")
    if not raw:
        return "utf-8"
    message = Message()
    message["content-type"] = raw
    charset = message.get_content_charset()
    return charset or "utf-8"


def _is_attachment(part: dict[str, Any]) -> bool:
    filename = part.get("filename", "")
    if filename is None:
        filename = ""
    if not isinstance(filename, str) or len(filename) > 512:
        raise GmailBootstrapContractError()
    if filename:
        return True
    disposition = _headers(part).get("content-disposition", "")
    return disposition.casefold().lstrip().startswith("attachment")


def _decode_base64url(
    value: object,
    *,
    max_bytes: int,
) -> bytes:
    if not isinstance(value, str):
        raise GmailBootstrapContractError()
    encoded_limit = 4 * ((max_bytes + 2) // 3) + 8
    if len(value) > encoded_limit:
        raise GmailBootstrapContractError()
    try:
        raw = value.encode("ascii")
        padding = b"=" * ((-len(raw)) % 4)
        decoded = base64.b64decode(
            raw + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise GmailBootstrapContractError() from None
    if len(decoded) > max_bytes:
        raise GmailBootstrapContractError()
    return decoded


def _mime_projection(depth: int) -> str:
    fields = (
        "mimeType,filename,headers(name,value),"
        "body(size,data,attachmentId)"
    )
    if depth > 1:
        fields += f",parts({_mime_projection(depth - 1)})"
    return fields


class GmailBootstrapApiReader(GmailApiReader):
    """Explicit body-observing Gmail reader used only by Semantic Bootstrap."""

    MAX_SNAPSHOT_MESSAGES = 100

    def profile_email_address(self) -> str:
        payload = self._get_json(
            "/profile",
            query={"fields": "emailAddress"},
            max_response_bytes=16 * 1024,
        )
        value = payload.get("emailAddress")
        if (
            not isinstance(value, str)
            or not 3 <= len(value) <= 320
            or "@" not in value
            or any(character.isspace() for character in value)
        ):
            raise GmailBootstrapContractError()
        return value.strip().casefold()

    def snapshot_message_ids(
        self,
        *,
        max_results: int,
    ) -> tuple[str, ...]:
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= self.MAX_SNAPSHOT_MESSAGES
        ):
            raise ValueError("GMAIL_BOOTSTRAP_SNAPSHOT_LIMIT_OUT_OF_RANGE")
        payload = self._get_json(
            "/messages",
            query={
                "maxResults": max_results,
                "includeSpamTrash": "false",
                "fields": "messages(id),nextPageToken,resultSizeEstimate",
            },
            max_response_bytes=256 * 1024,
        )
        raw_messages = payload.get("messages", [])
        if raw_messages is None:
            raw_messages = []
        if (
            not isinstance(raw_messages, list)
            or len(raw_messages) > max_results
        ):
            raise GmailBootstrapContractError()

        result: list[str] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                raise GmailBootstrapContractError()
            message_id = item.get("id")
            if (
                not isinstance(message_id, str)
                or not message_id
                or len(message_id) > 240
                or message_id in result
            ):
                raise GmailBootstrapContractError()
            result.append(message_id)
        return tuple(result)

    def _body_bytes(
        self,
        message_id: str,
        part: dict[str, Any],
        *,
        max_bytes: int,
    ) -> bytes:
        body = part.get("body")
        if body is None:
            return b""
        if not isinstance(body, dict):
            raise GmailBootstrapContractError()

        size = body.get("size", 0)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > max_bytes
        ):
            raise GmailBootstrapContractError()

        encoded = body.get("data")
        attachment_id = body.get("attachmentId")
        if encoded is not None and attachment_id is not None:
            raise GmailBootstrapContractError()
        if encoded is not None:
            decoded = _decode_base64url(
                encoded,
                max_bytes=max_bytes,
            )
            if size and len(decoded) != size:
                raise GmailBootstrapContractError()
            return decoded
        if attachment_id is not None:
            if (
                not isinstance(attachment_id, str)
                or not attachment_id
                or len(attachment_id) > 2048
                or size <= 0
            ):
                raise GmailBootstrapContractError()
            try:
                return self.read_attachment(
                    message_id,
                    attachment_id,
                    expected_size=size,
                    max_bytes=max_bytes,
                )
            except GmailConnectorError:
                raise GmailBootstrapContractError() from None
        return b""

    def _plain_text(
        self,
        message_id: str,
        root: object,
        *,
        max_body_bytes: int,
        max_mime_depth: int,
    ) -> str:
        if not isinstance(root, dict):
            raise GmailBootstrapContractError()
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or not 1 <= max_body_bytes <= 2 * 1024 * 1024
        ):
            raise ValueError("GMAIL_BOOTSTRAP_BODY_LIMIT_OUT_OF_RANGE")
        if (
            isinstance(max_mime_depth, bool)
            or not isinstance(max_mime_depth, int)
            or not 1 <= max_mime_depth <= 32
        ):
            raise ValueError("GMAIL_BOOTSTRAP_MIME_DEPTH_OUT_OF_RANGE")

        remaining = max_body_bytes
        fragments: list[str] = []
        nodes = 0

        def walk(part: dict[str, Any], depth: int) -> None:
            nonlocal remaining, nodes
            nodes += 1
            if nodes > 1024:
                raise GmailBootstrapContractError()

            mime_type = part.get("mimeType")
            if (
                not isinstance(mime_type, str)
                or not mime_type
                or len(mime_type) > 160
            ):
                raise GmailBootstrapContractError()
            normalized = mime_type.casefold()
            parts = part.get("parts")
            if parts is not None:
                if not isinstance(parts, list) or len(parts) > 512:
                    raise GmailBootstrapContractError()
                if depth >= max_mime_depth and parts:
                    raise GmailBootstrapContractError()

            if (
                normalized == "text/plain"
                and not _is_attachment(part)
            ):
                if remaining <= 0:
                    raise GmailBootstrapContractError()
                raw = self._body_bytes(
                    message_id,
                    part,
                    max_bytes=remaining,
                )
                remaining -= len(raw)
                if raw:
                    charset = _content_charset(part)
                    try:
                        text = raw.decode(charset, errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                    text = text.replace("\x00", "").strip()
                    if text:
                        fragments.append(text)

            if parts:
                for child in parts:
                    if not isinstance(child, dict):
                        raise GmailBootstrapContractError()
                    walk(child, depth + 1)

        walk(root, 1)
        return "\n\n".join(fragments)

    def read_bootstrap_message(
        self,
        message_id: str,
        *,
        max_body_bytes: int,
        max_mime_depth: int,
    ) -> GmailMessage:
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 240
        ):
            raise ValueError("GMAIL_MESSAGE_ID_REQUIRED")
        payload = self._get_json(
            f"/messages/{urllib_parse.quote(message_id, safe='')}",
            query={
                "format": "full",
                "fields": (
                    "id,threadId,internalDate,"
                    f"payload({_mime_projection(max_mime_depth)})"
                ),
            },
            max_response_bytes=(
                4 * max_body_bytes + 512 * 1024
            ),
        )

        # Reuse the already-certified Gmail metadata parser over the
        # same full payload; no second provider request is needed.
        try:
            base = _gmail_api_payload_to_message(payload)
        except GmailConnectorError:
            raise GmailBootstrapContractError() from None
        body = self._plain_text(
            message_id,
            payload.get("payload"),
            max_body_bytes=max_body_bytes,
            max_mime_depth=max_mime_depth,
        )
        return replace(
            base,
            body=body,
            body_observed=True,
            attachments=(),
            attachments_observed=False,
        )


class GmailBootstrapHistoryAdapter:
    """HistoryAdapter projection over one frozen, bounded Gmail mailbox snapshot."""

    MAILBOX_KEY = "gmail:mailbox"
    CURSOR_PREFIX = "gbv0."

    def __init__(
        self,
        *,
        reader: GmailBootstrapApiReader,
        account_email: str,
        snapshot_limit: int = 100,
        max_body_bytes: int = 256 * 1024,
        max_mime_depth: int = 12,
    ):
        if (
            not isinstance(account_email, str)
            or not account_email
            or "@" not in account_email
            or len(account_email) > 320
            or any(character.isspace() for character in account_email)
        ):
            raise GmailBootstrapContractError()
        if (
            isinstance(snapshot_limit, bool)
            or not isinstance(snapshot_limit, int)
            or not 1 <= snapshot_limit <= 100
        ):
            raise GmailBootstrapContractError()
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or not 1 <= max_body_bytes <= 2 * 1024 * 1024
            or isinstance(max_mime_depth, bool)
            or not isinstance(max_mime_depth, int)
            or not 1 <= max_mime_depth <= 32
        ):
            raise GmailBootstrapContractError()
        self.reader = reader
        self.account_email = account_email.strip().casefold()
        self.snapshot_limit = snapshot_limit
        self.max_body_bytes = max_body_bytes
        self.max_mime_depth = max_mime_depth

    def list_chats(self) -> list[dict[str, Any]]:
        return [
            {
                "external_thread_key": self.MAILBOX_KEY,
                "thread_type": "DIRECT",
                "title": "Gmail",
            }
        ]

    @classmethod
    def _encode_cursor(
        cls,
        *,
        message_ids: tuple[str, ...],
        offset: int,
    ) -> str:
        raw = _canonical_json(
            {
                "v": 1,
                "message_ids": list(message_ids),
                "offset": offset,
            }
        )
        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return cls.CURSOR_PREFIX + token

    @classmethod
    def _decode_cursor(
        cls,
        value: str,
    ) -> tuple[tuple[str, ...], int]:
        if (
            not isinstance(value, str)
            or not value.startswith(cls.CURSOR_PREFIX)
            or len(value) > 8192
        ):
            raise GmailBootstrapSnapshotInvalid()
        token = value[len(cls.CURSOR_PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(
                token + "=" * (-len(token) % 4)
            )
            decoded = json.loads(raw.decode("utf-8"))
        except Exception:
            raise GmailBootstrapSnapshotInvalid() from None
        if not isinstance(decoded, dict) or decoded.get("v") != 1:
            raise GmailBootstrapSnapshotInvalid()
        raw_ids = decoded.get("message_ids")
        offset = decoded.get("offset")
        if (
            not isinstance(raw_ids, list)
            or len(raw_ids) > 100
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 240
                for item in raw_ids
            )
            or len(raw_ids) != len(set(raw_ids))
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= len(raw_ids)
        ):
            raise GmailBootstrapSnapshotInvalid()
        return tuple(raw_ids), offset

    def _thread_type(self, message: GmailMessage) -> str:
        addresses = {
            address.strip().casefold()
            for address in (
                message.to + message.cc + message.bcc
            )
            if isinstance(address, str)
            and address.strip()
        }
        _name, sender = parseaddr(message.sender)
        if sender:
            addresses.add(sender.strip().casefold())
        addresses.discard(self.account_email)
        return "GROUP" if len(addresses) > 1 else "DIRECT"

    def _payload(self, message: GmailMessage) -> dict[str, Any]:
        sender_name, sender_address = parseaddr(message.sender)
        sender_key = sender_address.strip().casefold()
        if (
            not sender_key
            or "@" not in sender_key
            or len(sender_key) > 240
            or any(character.isspace() for character in sender_key)
        ):
            raise GmailBootstrapContractError()
        try:
            sent_at = datetime.fromisoformat(
                message.email_ts.replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            raise GmailBootstrapContractError() from None
        if sent_at.tzinfo is None:
            raise GmailBootstrapContractError()
        sent_at = sent_at.astimezone(UTC)

        thread_id = message.thread_id or message.message_id
        thread_key = f"gmail:{thread_id}"
        if len(thread_key) > 240:
            raise GmailBootstrapContractError()

        subject = message.subject.strip()
        body = message.body.strip()
        if subject and body:
            text = f"Assunto: {subject}\n\n{body}"
        elif subject:
            text = f"Assunto: {subject}"
        else:
            text = body or None

        return {
            "source_message_id": message.message_id,
            "external_thread_key": thread_key,
            "thread_type": self._thread_type(message),
            "title": subject[:240] or None,
            "external_sender_key": sender_key,
            "sender_display_name": sender_name.strip()[:240] or None,
            "sent_at": sent_at,
            "text": text,
            "type": "TEXT" if text else "UNSUPPORTED",
            "from_me": sender_key == self.account_email,
            "metadata": {
                "provider": "gmail",
                "provider_thread_id": thread_id,
                "body_observed": True,
                "attachments_observed": False,
                "to_count": len(message.to),
                "cc_count": len(message.cc),
                "bcc_count": len(message.bcc),
            },
        }

    def fetch_messages(
        self,
        chat_key: str,
        limit: int,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if chat_key != self.MAILBOX_KEY:
            raise GmailBootstrapContractError()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > self.snapshot_limit
        ):
            raise GmailBootstrapContractError()

        if cursor is None:
            message_ids = self.reader.snapshot_message_ids(
                max_results=self.snapshot_limit,
            )
            offset = 0
        else:
            message_ids, offset = self._decode_cursor(cursor)
            if len(message_ids) > self.snapshot_limit:
                raise GmailBootstrapSnapshotInvalid()

        selected = message_ids[offset:offset + limit]
        messages = [
            self._payload(
                self.reader.read_bootstrap_message(
                    message_id,
                    max_body_bytes=self.max_body_bytes,
                    max_mime_depth=self.max_mime_depth,
                )
            )
            for message_id in selected
        ]
        next_offset = offset + len(selected)
        next_cursor = (
            self._encode_cursor(
                message_ids=message_ids,
                offset=next_offset,
            )
            if next_offset < len(message_ids)
            else None
        )
        return {
            "messages": messages,
            "next_cursor": next_cursor,
        }


__all__ = [
    "GmailBootstrapApiReader",
    "GmailBootstrapContractError",
    "GmailBootstrapError",
    "GmailBootstrapHistoryAdapter",
    "GmailBootstrapSnapshotInvalid",
]
