from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from attention_router.infrastructure.db import Base


JsonType = JSON().with_variant(JSONB, "postgresql")


class ArtifactRow(Base):
    """Canonical tenant-local identity for one immutable content blob."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("resources.id"), nullable=False, unique=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(160), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    storage_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="AVAILABLE")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JsonType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_sha256", name="uq_artifact_tenant_sha256"),
        UniqueConstraint("tenant_id", "id", name="uq_artifact_tenant_id"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size_nonnegative"),
        CheckConstraint(
            "status in ('AVAILABLE','FAILED','DELETED')",
            name="ck_artifact_status",
        ),
        Index("ix_artifacts_tenant_created", "tenant_id", "created_at"),
    )


class ArtifactReceiptRow(Base):
    """One observed delivery of an artifact through a source channel."""

    __tablename__ = "artifact_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    source_account: Mapped[str] = mapped_column(String(120), nullable=False)
    external_receipt_id: Mapped[str] = mapped_column(String(240), nullable=False)
    sender_actor_id: Mapped[str | None] = mapped_column(String(120))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JsonType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_receipt_tenant_artifact",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_channel",
            "source_account",
            "external_receipt_id",
            name="uq_artifact_receipt_source",
        ),
        Index(
            "ix_artifact_receipts_tenant_artifact_received",
            "tenant_id",
            "artifact_id",
            "received_at",
        ),
        Index(
            "ix_artifact_receipts_tenant_received",
            "tenant_id",
            "received_at",
        ),
    )


class ArtifactUnderstandingRow(Base):
    """Derived, tenant-scoped understanding of one inbound Artifact."""

    __tablename__ = "artifact_understandings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64))
    inbound_event_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("inbound_events.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    visual_description: Mapped[str | None] = mapped_column(Text)
    key_facts_json: Mapped[list[str]] = mapped_column(
        "key_facts", JsonType, nullable=False, default=list
    )
    language: Mapped[str | None] = mapped_column(String(40))
    text_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_reference: Mapped[str | None] = mapped_column(String(180))
    error_code: Mapped[str | None] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_understanding_tenant_artifact",
        ),
        UniqueConstraint(
            "tenant_id",
            "inbound_event_id",
            name="uq_artifact_understanding_tenant_event",
        ),
        CheckConstraint(
            "status in ('PENDING','PROCESSING','READY','FAILED')",
            name="ck_artifact_understanding_status",
        ),
        CheckConstraint(
            "content_kind in ('IMAGE','DOCUMENT')",
            name="ck_artifact_understanding_kind",
        ),
        CheckConstraint(
            "(status='READY' and summary_text is not null) or "
            "(status<>'READY' and summary_text is null)",
            name="ck_artifact_understanding_ready_summary",
        ),
        Index(
            "ix_artifact_understanding_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_artifact_understanding_tenant_artifact",
            "tenant_id",
            "artifact_id",
        ),
    )


__all__ = ["ArtifactReceiptRow", "ArtifactRow", "ArtifactUnderstandingRow"]
