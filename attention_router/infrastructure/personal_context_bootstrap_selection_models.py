"""Operator-staged, owner-bound history selections; possession grants no authority."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import JsonType


class PersonalContextBootstrapSelectionRow(Base):
    __tablename__ = "personal_context_bootstrap_selections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    owner_human_identity_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("human_identities.id"), nullable=False
    )
    chat_keys: Mapped[list] = mapped_column(JsonType, nullable=False)
    display_chats: Mapped[list] = mapped_column(JsonType, nullable=False)
    expected_consent_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    processing_budget: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bootstrap_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("personal_context_bootstrap_runs.id")
    )
    confirmation_fingerprint: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index(
            "ix_pc_bootstrap_selection_owner", "tenant_id",
            "owner_human_identity_id", "expires_at",
        ),
        CheckConstraint("expires_at > created_at", name="ck_pc_bootstrap_selection_expiry"),
        CheckConstraint(
            "(consumed_at IS NULL AND bootstrap_run_id IS NULL AND confirmation_fingerprint IS NULL) "
            "OR (consumed_at IS NOT NULL AND bootstrap_run_id IS NOT NULL AND confirmation_fingerprint IS NOT NULL)",
            name="ck_pc_bootstrap_selection_consumption",
        ),
    )
