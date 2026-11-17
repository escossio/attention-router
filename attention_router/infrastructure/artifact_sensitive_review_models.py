"""Separate data-governance reviews. Not an execution authorization."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ArtifactSensitiveReviewRow(Base):
    __tablename__ = "artifact_sensitive_reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    understanding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_inbound_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only classification, references, status and audit. Never copy OCR/card numbers.
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    expected_owner_human_identity_id: Mapped[str | None] = mapped_column(String(64))
    decision_human_identity_id: Mapped[str | None] = mapped_column(String(64))
    decision_device_id: Mapped[str | None] = mapped_column(String(64))
    decision_client_session_id: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.id"],
            name="fk_artifact_sensitive_review_artifact_scope",
        ),
        ForeignKeyConstraint(
            ["understanding_id"], ["artifact_understandings.id"],
            name="fk_artifact_sensitive_review_understanding",
        ),
        ForeignKeyConstraint(
            ["source_inbound_event_id"], ["inbound_events.id"],
            name="fk_artifact_sensitive_review_receipt",
        ),
        UniqueConstraint("tenant_id", "understanding_id", name="uq_artifact_sensitive_review_source"),
        CheckConstraint(
            "category IN ('PAYMENT_CARD','IDENTITY_DOCUMENT','ACCESS_SECRET','CONTACT_CARD')",
            name="ck_artifact_sensitive_review_category",
        ),
        CheckConstraint(
            "state IN ('PENDING','UNASSIGNED','APPROVED_PRIVATE','ARCHIVE_ONLY','EXPIRED')",
            name="ck_artifact_sensitive_review_state",
        ),
        CheckConstraint(
            "expires_at > issued_at", name="ck_artifact_sensitive_review_time",
        ),
        Index(
            "ix_artifact_sensitive_review_owner_pending",
            "tenant_id", "expected_owner_human_identity_id", "state",
        ),
    )


__all__ = ["ArtifactSensitiveReviewRow"]
