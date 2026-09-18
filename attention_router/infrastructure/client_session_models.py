"""Persistence rows for V0.3C client-session authority."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, PrimaryKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ClientSessionChallengeRow(Base):
    __tablename__ = "client_session_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("client_devices.id", name="fk_client_session_challenge_device"), nullable=False
    )
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_client_session_challenge_human_identity"),
        nullable=False,
    )
    requested_tenant_id: Mapped[str | None] = mapped_column(String(64))
    challenge_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_client_session_challenges"),
        UniqueConstraint("challenge_digest", name="uq_client_session_challenge_digest"),
        CheckConstraint(
            "state IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_client_session_challenge_state",
        ),
        CheckConstraint("expires_at > created_at", name="ck_client_session_challenge_lifetime"),
        Index("ix_client_session_challenge_device_state", "device_id", "state"),
        Index("ix_client_session_challenge_human_state", "human_identity_id", "state"),
    )


class ClientSessionRow(Base):
    __tablename__ = "client_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_challenge_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("client_session_challenges.id", name="fk_client_session_source_challenge"),
        nullable=False,
    )
    human_identity_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("human_identities.id", name="fk_client_session_human_identity"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("client_devices.id", name="fk_client_session_device"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", name="fk_client_session_tenant"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_client_sessions"),
        UniqueConstraint("token_digest", name="uq_client_session_token_digest"),
        UniqueConstraint("source_challenge_id", name="uq_client_session_source_challenge"),
        CheckConstraint("state IN ('ACTIVE', 'REVOKED')", name="ck_client_session_state"),
        CheckConstraint("expires_at > created_at", name="ck_client_session_lifetime"),
        CheckConstraint(
            "(state = 'ACTIVE' AND revoked_at IS NULL) OR (state = 'REVOKED' AND revoked_at IS NOT NULL)",
            name="ck_client_session_revocation_state",
        ),
        Index("ix_client_session_human_tenant_state", "human_identity_id", "tenant_id", "state"),
        Index("ix_client_session_device_state", "device_id", "state"),
    )
