"""Application seam between untrusted bytes and Artifact Plane registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.artifacts import (
    ArtifactReceiptInput,
    ArtifactRegistrationResult,
    register_artifact_receipt,
)
from attention_router.core.tenancy import TenantScopeError
from attention_router.infrastructure.artifact_models import ArtifactRow
from attention_router.infrastructure.artifact_store import (
    ArtifactObjectStore,
    ArtifactStoreError,
    StoredArtifactObject,
)


class ArtifactStorageError(ValueError):
    pass


class ArtifactStorageBackendUnavailable(ArtifactStorageError):
    pass


class ArtifactUnavailable(ArtifactStorageError):
    pass
@dataclass(frozen=True, slots=True)
class ArtifactStageInput:
    tenant_id: str
    data: bytes = field(repr=False)
    artifact_kind: str = "DOCUMENT"
    mime_type: str = "application/octet-stream"
    source_channel: str = "unknown"
    external_receipt_id: str = ""
    received_at: datetime | None = None
    source_account: str = "default"
    sender_actor_id: str | None = None
    original_filename: str | None = None
    artifact_metadata: dict[str, Any] | None = None
    receipt_metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ArtifactStageResult:
    stored: StoredArtifactObject
    registration: ArtifactRegistrationResult


def _stage_received_at(value: datetime | None) -> datetime:
    if value is None:
        raise ArtifactStorageError(
            "ARTIFACT_STAGE_RECEIVED_AT_REQUIRED"
        )
    return value
def stage_artifact_receipt(
    session: Session,
    store: ArtifactObjectStore,
    item: ArtifactStageInput,
) -> ArtifactStageResult:
    """Persist bytes before registering their canonical identity/provenance.

    The caller owns the database transaction. A later DB rollback may leave an
    unreferenced immutable object; inline deletion is intentionally forbidden.
    """

    stored = store.put_bytes(item.tenant_id, item.data)
    registration = register_artifact_receipt(
        session,
        ArtifactReceiptInput(
            tenant_id=item.tenant_id,
            content_sha256=stored.content_sha256,
            artifact_kind=item.artifact_kind,
            mime_type=item.mime_type,
            size_bytes=stored.size_bytes,
            storage_provider=stored.storage_provider,
            storage_reference=stored.storage_reference,
            source_channel=item.source_channel,
            external_receipt_id=item.external_receipt_id,
            received_at=_stage_received_at(item.received_at),
            source_account=item.source_account,
            sender_actor_id=item.sender_actor_id,
            original_filename=item.original_filename,
            artifact_metadata=item.artifact_metadata,
            receipt_metadata=item.receipt_metadata,
        ),
    )
    return ArtifactStageResult(
        stored=stored,
        registration=registration,
    )
def read_artifact_bytes(
    session: Session,
    stores: Mapping[str, ArtifactObjectStore],
    *,
    tenant_id: str,
    artifact_id: str,
) -> bytes:
    """Read an AVAILABLE artifact without crossing its tenant boundary."""

    normalized_tenant = tenant_id.strip()
    normalized_artifact = artifact_id.strip()
    if not normalized_tenant or not normalized_artifact:
        raise TenantScopeError("ARTIFACT_NOT_FOUND")

    artifact = session.scalar(
        select(ArtifactRow).where(
            ArtifactRow.tenant_id == normalized_tenant,
            ArtifactRow.id == normalized_artifact,
        )
    )
    if artifact is None:
        raise TenantScopeError("ARTIFACT_NOT_FOUND")
    if artifact.status != "AVAILABLE":
        raise ArtifactUnavailable("ARTIFACT_NOT_AVAILABLE")

    provider = artifact.storage_provider.casefold()
    store = stores.get(provider)
    if store is None:
        raise ArtifactStorageBackendUnavailable(
            "ARTIFACT_STORAGE_BACKEND_UNAVAILABLE"
        )
    if store.storage_provider.casefold() != provider:
        raise ArtifactStorageBackendUnavailable(
            "ARTIFACT_STORAGE_BACKEND_MISMATCH"
        )

    try:
        return store.read_bytes(
            normalized_tenant,
            artifact.storage_reference,
            expected_sha256=artifact.content_sha256,
            expected_size=artifact.size_bytes,
        )
    except ArtifactStoreError as exc:
        raise ArtifactUnavailable(
            "ARTIFACT_STORAGE_READ_FAILED"
        ) from exc

__all__ = [
    "ArtifactStageInput",
    "ArtifactStageResult",
    "ArtifactStorageBackendUnavailable",
    "ArtifactStorageError",
    "ArtifactUnavailable",
    "read_artifact_bytes",
    "stage_artifact_receipt",
]
