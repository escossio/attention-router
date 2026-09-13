"""Provider-neutral Human Identity persistence rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class HumanIdentityRow(Base):
    __tablename__ = "human_identities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("id", name="pk_human_identities"),)


class ExternalIdentityBindingRow(Base):
    __tablename__ = "external_identity_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_external_identity_binding_human_identity"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_external_identity_bindings"),
        UniqueConstraint(
            "provider", "subject", name="uq_external_identity_provider_subject",
        ),
    )


class HumanAuthTransactionRow(Base):
    __tablename__ = "human_auth_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    nonce_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_human_identity_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_human_auth_transaction_human_identity"),
        nullable=True,
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_human_auth_transactions"),
        UniqueConstraint("nonce_digest", name="uq_human_auth_transaction_nonce_digest"),
        CheckConstraint(
            "state IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_human_auth_transaction_state",
        ),
    )
