"""Encrypted provider authorization installation persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ProviderAuthorizationRow(Base):
    __tablename__ = "provider_authorizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False
    )
    human_identity_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("human_identities.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    product: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_account_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_scopes: Mapped[list] = mapped_column(JSON, nullable=False)
    secret_nonce_b64url: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ciphertext_b64url: Mapped[str] = mapped_column(String(8192), nullable=False)
    secret_key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    integration_binding_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("integration_bindings.id"), nullable=False
    )
    integration_credential_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("integration_credentials.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_provider_authorizations"),
        UniqueConstraint("slot_key", name="uq_provider_authorization_slot"),
        CheckConstraint("provider = 'GOOGLE'", name="ck_provider_authorization_provider"),
        CheckConstraint("product = 'GMAIL'", name="ck_provider_authorization_product"),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_provider_authorization_status",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL) OR "
            "(status = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_provider_authorization_revocation",
        ),
        Index(
            "ix_provider_authorization_owner_product",
            "tenant_id",
            "human_identity_id",
            "provider",
            "product",
            "status",
        ),
    )
