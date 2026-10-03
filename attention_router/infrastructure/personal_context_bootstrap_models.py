"""Persistence rows for Personal Context V2B semantic bootstrap."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import JsonType


class PersonalContextBootstrapRunRow(Base):
    __tablename__ = "personal_context_bootstrap_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_pc_bootstrap_run_tenant"),
        nullable=False,
    )
    owner_human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_pc_bootstrap_run_human_identity"),
        nullable=False,
    )
    represented_owner_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(160))
    source_selection: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    consent_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_control: Mapped[str] = mapped_column(
        String(16), nullable=False, default="NONE", server_default="NONE"
    )
    resume_cursor: Mapped[dict | None] = mapped_column(JsonType)
    processing_budget: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    progress: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_personal_context_bootstrap_runs"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_personal_context_bootstrap_run_idempotency",
        ),
        CheckConstraint(
            "state IN ('CREATED','QUEUED','RUNNING','PAUSED','COMPLETED','CANCELLED','FAILED')",
            name="ck_personal_context_bootstrap_run_state",
        ),
        CheckConstraint(
            "requested_control IN ('NONE','PAUSE','CANCEL')",
            name="ck_personal_context_bootstrap_run_control",
        ),
        CheckConstraint(
            "length(trim(consent_ref)) > 0",
            name="ck_personal_context_bootstrap_consent_nonempty",
        ),
        Index(
            "ix_personal_context_bootstrap_owner_state",
            "tenant_id",
            "represented_owner_actor_key",
            "state",
        ),
        Index(
            "ix_personal_context_bootstrap_human_state",
            "owner_human_identity_id",
            "state",
        ),
    )


class PersonalContextBootstrapBatchRow(Base):
    __tablename__ = "personal_context_bootstrap_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bootstrap_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "personal_context_bootstrap_runs.id",
            name="fk_pc_bootstrap_batch_run",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    cursor_before: Mapped[dict | None] = mapped_column(JsonType)
    cursor_after: Mapped[dict | None] = mapped_column(JsonType)
    metrics: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_personal_context_bootstrap_batches"),
        UniqueConstraint(
            "bootstrap_run_id",
            "ordinal",
            name="uq_personal_context_bootstrap_batch_ordinal",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_personal_context_bootstrap_batch_idempotency",
        ),
        CheckConstraint(
            "ordinal > 0",
            name="ck_personal_context_bootstrap_batch_ordinal_positive",
        ),
        CheckConstraint(
            "state IN ('CREATED','RUNNING','COMPLETED','FAILED')",
            name="ck_personal_context_bootstrap_batch_state",
        ),
        Index(
            "ix_personal_context_bootstrap_batch_run_state",
            "bootstrap_run_id",
            "state",
            "ordinal",
        ),
    )


__all__ = [
    "PersonalContextBootstrapBatchRow",
    "PersonalContextBootstrapRunRow",
]
