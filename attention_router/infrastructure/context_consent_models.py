"""Persistent source decisions for staged contextual reads."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ContextConsentScopeRow(Base):
    __tablename__ = "context_consent_scopes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    incarnation: Mapped[str] = mapped_column(String(36), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = (CheckConstraint("revision >= 0", name="ck_context_consent_revision"),)


class ContextSourceConsentRow(Base):
    __tablename__ = "context_source_consents"

    scope_id: Mapped[str] = mapped_column(ForeignKey("context_consent_scopes.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_ref: Mapped[str | None] = mapped_column(String(120))
    conversation_id: Mapped[str | None] = mapped_column(String(64))
    receipt_id: Mapped[str | None] = mapped_column(String(64))
    message_revision: Mapped[str | None] = mapped_column(String(36))
    receipt_revision: Mapped[str | None] = mapped_column(String(36))
    content_digest: Mapped[str | None] = mapped_column(String(64))
    sensitivity: Mapped[str | None] = mapped_column(String(20))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint(
        "revoked_at IS NOT NULL OR (decision_ref IS NOT NULL AND conversation_id IS NOT NULL "
        "AND receipt_id IS NOT NULL AND message_revision IS NOT NULL AND receipt_revision IS NOT NULL "
        "AND content_digest IS NOT NULL AND sensitivity IS NOT NULL AND starts_at IS NOT NULL "
        "AND expires_at IS NOT NULL AND starts_at < expires_at AND retention IS NOT NULL)",
        name="ck_context_source_consent_complete",
    ),)
