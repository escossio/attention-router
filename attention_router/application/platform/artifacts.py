from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.tenancy import TenantScopeError
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
from attention_router.infrastructure.models import ResourceRow, TenantRow


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RESOURCE_TYPE = "ARTIFACT"


class ArtifactRegistrationError(ValueError):
    pass


class ArtifactReceiptConflict(ArtifactRegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactReceiptInput:
    tenant_id: str
    content_sha256: str
    artifact_kind: str
    mime_type: str
    size_bytes: int
    storage_provider: str
    storage_reference: str
    source_channel: str
    external_receipt_id: str
    received_at: datetime
    source_account: str = "default"
    sender_actor_id: str | None = None
    original_filename: str | None = None
    artifact_metadata: dict[str, Any] | None = None
    receipt_metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRegistrationResult:
    artifact: ArtifactRow
    receipt: ArtifactReceiptRow
    artifact_created: bool
    receipt_created: bool


def _required(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ArtifactRegistrationError(reason)
    return normalized


def _normalize_input(item: ArtifactReceiptInput) -> ArtifactReceiptInput:
    tenant_id = _required(item.tenant_id, "ARTIFACT_TENANT_REQUIRED")
    content_sha256 = item.content_sha256.strip().casefold()
    if not _SHA256_RE.fullmatch(content_sha256):
        raise ArtifactRegistrationError("ARTIFACT_SHA256_INVALID")
    if item.size_bytes < 0:
        raise ArtifactRegistrationError("ARTIFACT_SIZE_INVALID")
    if item.received_at.tzinfo is None or item.received_at.utcoffset() is None:
        raise ArtifactRegistrationError("ARTIFACT_RECEIVED_AT_TIMEZONE_REQUIRED")

    source_account = item.source_account.strip() or "default"
    original_filename = item.original_filename.strip() if item.original_filename else None
    sender_actor_id = item.sender_actor_id.strip() if item.sender_actor_id else None

    return ArtifactReceiptInput(
        tenant_id=tenant_id,
        content_sha256=content_sha256,
        artifact_kind=_required(item.artifact_kind, "ARTIFACT_KIND_REQUIRED").upper(),
        mime_type=_required(item.mime_type, "ARTIFACT_MIME_TYPE_REQUIRED").casefold(),
        size_bytes=item.size_bytes,
        storage_provider=_required(
            item.storage_provider, "ARTIFACT_STORAGE_PROVIDER_REQUIRED"
        ).casefold(),
        storage_reference=_required(
            item.storage_reference, "ARTIFACT_STORAGE_REFERENCE_REQUIRED"
        ),
        source_channel=_required(
            item.source_channel, "ARTIFACT_SOURCE_CHANNEL_REQUIRED"
        ).casefold(),
        external_receipt_id=_required(
            item.external_receipt_id, "ARTIFACT_EXTERNAL_RECEIPT_ID_REQUIRED"
        ),
        received_at=item.received_at.astimezone(timezone.utc),
        source_account=source_account.casefold(),
        sender_actor_id=sender_actor_id,
        original_filename=original_filename,
        artifact_metadata=dict(item.artifact_metadata or {}),
        receipt_metadata=dict(item.receipt_metadata or {}),
    )


def _receipt_for_source(
    session: Session,
    *,
    tenant_id: str,
    source_channel: str,
    source_account: str,
    external_receipt_id: str,
) -> ArtifactReceiptRow | None:
    return session.scalar(
        select(ArtifactReceiptRow).where(
            ArtifactReceiptRow.tenant_id == tenant_id,
            ArtifactReceiptRow.source_channel == source_channel,
            ArtifactReceiptRow.source_account == source_account,
            ArtifactReceiptRow.external_receipt_id == external_receipt_id,
        )
    )


def _artifact_for_hash(
    session: Session,
    *,
    tenant_id: str,
    content_sha256: str,
) -> ArtifactRow | None:
    return session.scalar(
        select(ArtifactRow).where(
            ArtifactRow.tenant_id == tenant_id,
            ArtifactRow.content_sha256 == content_sha256,
        )
    )


def register_artifact_receipt(
    session: Session,
    item: ArtifactReceiptInput,
) -> ArtifactRegistrationResult:
    """Register one source receipt while deduplicating content inside one tenant.

    The caller owns the transaction. The artifact is canonical evidence; receipts
    preserve every observed source delivery. Content is never deduplicated across
    tenant boundaries.
    """

    item = _normalize_input(item)
    if session.get(TenantRow, item.tenant_id) is None:
        raise TenantScopeError("TENANT_NOT_FOUND:artifact")

    existing_receipt = _receipt_for_source(
        session,
        tenant_id=item.tenant_id,
        source_channel=item.source_channel,
        source_account=item.source_account,
        external_receipt_id=item.external_receipt_id,
    )
    if existing_receipt is not None:
        artifact = session.get(ArtifactRow, existing_receipt.artifact_id)
        if artifact is None or artifact.tenant_id != item.tenant_id:
            raise ArtifactReceiptConflict("ARTIFACT_RECEIPT_TARGET_INVALID")
        if artifact.content_sha256 != item.content_sha256:
            raise ArtifactReceiptConflict("ARTIFACT_RECEIPT_CONTENT_CONFLICT")
        return ArtifactRegistrationResult(
            artifact=artifact,
            receipt=existing_receipt,
            artifact_created=False,
            receipt_created=False,
        )

    artifact = _artifact_for_hash(
        session,
        tenant_id=item.tenant_id,
        content_sha256=item.content_sha256,
    )
    artifact_created = artifact is None

    if artifact is None:
        stamp = now_utc()
        artifact_id = new_id()
        resource_id = new_id()
        canonical_name = f"artifact:{item.content_sha256}"
        resource_collision = session.scalar(
            select(ResourceRow).where(
                ResourceRow.tenant_id == item.tenant_id,
                ResourceRow.resource_type == _ARTIFACT_RESOURCE_TYPE,
                ResourceRow.canonical_name == canonical_name,
            )
        )
        if resource_collision is not None:
            raise ArtifactRegistrationError("ARTIFACT_RESOURCE_COLLISION")

        resource = ResourceRow(
            id=resource_id,
            tenant_id=item.tenant_id,
            resource_type=_ARTIFACT_RESOURCE_TYPE,
            canonical_name=canonical_name,
            status="ACTIVE",
            metadata_json={
                "artifact_id": artifact_id,
                "content_sha256": item.content_sha256,
            },
            created_at=stamp,
            updated_at=stamp,
        )
        artifact = ArtifactRow(
            id=artifact_id,
            tenant_id=item.tenant_id,
            resource_id=resource_id,
            content_sha256=item.content_sha256,
            artifact_kind=item.artifact_kind,
            mime_type=item.mime_type,
            size_bytes=item.size_bytes,
            storage_provider=item.storage_provider,
            storage_reference=item.storage_reference,
            original_filename=item.original_filename,
            status="AVAILABLE",
            metadata_json=dict(item.artifact_metadata or {}),
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(resource)
        session.add(artifact)
        session.flush()
    elif artifact.size_bytes != item.size_bytes:
        raise ArtifactRegistrationError("ARTIFACT_CONTENT_IDENTITY_CONFLICT")

    receipt = ArtifactReceiptRow(
        id=new_id(),
        tenant_id=item.tenant_id,
        artifact_id=artifact.id,
        source_channel=item.source_channel,
        source_account=item.source_account,
        external_receipt_id=item.external_receipt_id,
        sender_actor_id=item.sender_actor_id,
        received_at=item.received_at,
        metadata_json=dict(item.receipt_metadata or {}),
        created_at=now_utc(),
    )
    session.add(receipt)
    session.flush()

    return ArtifactRegistrationResult(
        artifact=artifact,
        receipt=receipt,
        artifact_created=artifact_created,
        receipt_created=True,
    )


__all__ = [
    "ArtifactReceiptConflict",
    "ArtifactReceiptInput",
    "ArtifactRegistrationError",
    "ArtifactRegistrationResult",
    "register_artifact_receipt",
]
