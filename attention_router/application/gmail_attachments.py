"""Governed Gmail attachment staging into Artifact Plane."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Protocol

from sqlalchemy.orm import Session

from attention_router.application.platform.artifact_storage import (
    ArtifactStageInput,
    stage_artifact_receipt,
)
from attention_router.config import Settings
from attention_router.infrastructure.artifact_store import ArtifactObjectStore
from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorError,
    GmailMessage,
    GmailStagedAttachment,
)


class GmailAttachmentReader(Protocol):
    def read_message_with_attachments(
        self,
        message_id: str,
        *,
        max_attachments: int,
        max_mime_depth: int,
    ) -> GmailMessage: ...

    def read_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        expected_size: int,
        max_bytes: int,
    ) -> bytes: ...

def _artifact_kind(mime_type: str) -> str:
    normalized = mime_type.casefold()
    if normalized.startswith("image/"):
        return "IMAGE"
    if normalized.startswith("audio/"):
        return "AUDIO"
    if normalized.startswith("video/"):
        return "VIDEO"
    return "DOCUMENT"


def _received_at(message: GmailMessage) -> datetime:
    normalized = message.email_ts.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GmailConnectorError("GMAIL_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GmailConnectorError("GMAIL_TIMESTAMP_INVALID")
    return parsed


def _receipt_id(message_id: str, attachment_id: str) -> str:
    digest = hashlib.sha256(
        f"{message_id}\x00{attachment_id}".encode("utf-8")
    ).hexdigest()
    return f"gmail-att:{digest}"


def _validate_summary(
    summary: GmailAttachmentSummary,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    attachment_id = summary.attachment_id
    size = summary.size_bytes
    if (
        not isinstance(attachment_id, str)
        or not attachment_id
        or type(size) is not int
        or size < 0
        or size > max_bytes
    ):
        raise GmailConnectorError("GMAIL_ATTACHMENT_SUMMARY_INVALID")
    return attachment_id, size

class GmailAttachmentIngestor:
    """Download bounded opaque bytes and durably stage canonical artifacts."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory,
        store: ArtifactObjectStore,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._store = store

    def prepare_message(
        self,
        reader: GmailAttachmentReader,
        *,
        tenant_id: str,
        source_account: str,
        message_id: str,
    ) -> tuple[GmailMessage, tuple[GmailStagedAttachment, ...]]:
        message = reader.read_message_with_attachments(
            message_id,
            max_attachments=self.settings.gmail_attachment_max_count,
            max_mime_depth=self.settings.gmail_attachment_max_mime_depth,
        )
        if (
            message.message_id != message_id
            or message.body_observed
            or message.body
            or not message.attachments_observed
        ):
            raise GmailConnectorError("GMAIL_ATTACHMENT_MESSAGE_INVALID")

        total = 0
        downloaded: list[tuple[GmailAttachmentSummary, str, bytes]] = []
        for summary in message.attachments:
            attachment_id, size = _validate_summary(
                summary,
                max_bytes=self.settings.gmail_attachment_max_bytes,
            )
            total += size
            if total > self.settings.gmail_attachment_max_total_bytes:
                raise GmailConnectorError("GMAIL_ATTACHMENT_TOTAL_SIZE_EXCEEDED")
            data = reader.read_attachment(
                message_id,
                attachment_id,
                expected_size=size,
                max_bytes=self.settings.gmail_attachment_max_bytes,
            )
            if len(data) != size:
                raise GmailConnectorError("GMAIL_ATTACHMENT_SIZE_MISMATCH")
            downloaded.append((summary, attachment_id, data))

        staged: list[GmailStagedAttachment] = []
        received_at = _received_at(message)
        with self._session_factory.begin() as session:
            assert isinstance(session, Session)
            for index, (summary, attachment_id, data) in enumerate(downloaded):
                external_receipt_id = _receipt_id(message_id, attachment_id)
                result = stage_artifact_receipt(
                    session,
                    self._store,
                    ArtifactStageInput(
                        tenant_id=tenant_id,
                        data=data,
                        artifact_kind=_artifact_kind(summary.mime_type),
                        mime_type=summary.mime_type,
                        source_channel="channel.email",
                        external_receipt_id=external_receipt_id,
                        received_at=received_at,
                        source_account=source_account,
                        original_filename=summary.filename or None,
                        receipt_metadata={"attachment_index": index},
                    ),
                )
                artifact = result.registration.artifact
                staged.append(
                    GmailStagedAttachment(
                        attachment_id=attachment_id,
                        artifact_id=artifact.id,
                        external_receipt_id=external_receipt_id,
                        content_sha256=artifact.content_sha256,
                        filename=summary.filename,
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                        storage_provider=artifact.storage_provider,
                        storage_reference=artifact.storage_reference,
                    )
                )
        return message, tuple(staged)


__all__ = [
    "GmailAttachmentIngestor",
    "GmailAttachmentReader",
]
