"""Persistence rows for V0.3B pre-session device bootstrap authority."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import JsonType


class ClientTenantMembershipRow(Base):
    __tablename__ = "client_tenant_memberships"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_client_membership_human_identity"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_client_membership_tenant"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_client_tenant_memberships"),
        UniqueConstraint(
            "human_identity_id",
            "tenant_id",
            name="uq_client_membership_human_tenant",
        ),
        CheckConstraint(
            "role IN ('OWNER', 'ADMIN', 'MEMBER')",
            name="ck_client_membership_role",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')",
            name="ck_client_membership_status",
        ),
        Index(
            "ix_client_membership_human_status",
            "human_identity_id",
            "status",
        ),
    )


class ClientDeviceRow(Base):
    __tablename__ = "client_devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_client_device_human_identity"),
        nullable=False,
    )
    public_key_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    public_key_spki: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(24), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_client_devices"),
        UniqueConstraint(
            "public_key_fingerprint",
            name="uq_client_device_public_key_fingerprint",
        ),
        CheckConstraint("platform = 'ANDROID'", name="ck_client_device_platform"),
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_client_device_status",
        ),
        Index("ix_client_device_human_status", "human_identity_id", "status"),
    )


class DeviceBootstrapChallengeRow(Base):
    __tablename__ = "device_bootstrap_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    continuation_grant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "human_auth_continuation_grants.id",
            name="fk_device_bootstrap_continuation_grant",
        ),
        nullable=False,
    )
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_identities.id", name="fk_device_bootstrap_human_identity"),
        nullable=False,
    )
    public_key_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    public_key_spki: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    canonical_device_name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(24), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    challenge_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_device_bootstrap_challenges"),
        UniqueConstraint(
            "continuation_grant_id",
            name="uq_device_bootstrap_continuation_grant",
        ),
        UniqueConstraint(
            "challenge_digest",
            name="uq_device_bootstrap_challenge_digest",
        ),
        CheckConstraint("platform = 'ANDROID'", name="ck_device_bootstrap_platform"),
        CheckConstraint(
            "state IN ('PENDING', 'VERIFIED', 'REJECTED')",
            name="ck_device_bootstrap_state",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_device_bootstrap_lifetime",
        ),
        Index(
            "ix_device_bootstrap_human_state",
            "human_identity_id",
            "state",
        ),
    )
