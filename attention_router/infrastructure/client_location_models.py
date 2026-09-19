"""Persistence row for the V0.4A latest client location snapshot."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ClientLocationSnapshotRow(Base):
    __tablename__ = "client_location_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    human_identity_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "human_identities.id",
            name="fk_client_location_human_identity",
        ),
        nullable=False,
    )
    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("client_devices.id", name="fk_client_location_device"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_client_location_tenant"),
        nullable=False,
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float] = mapped_column(Float, nullable=False)
    precision: Mapped[str | None] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_client_location_snapshots"),
        UniqueConstraint(
            "device_id",
            "tenant_id",
            name="uq_client_location_device_tenant",
        ),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90",
            name="ck_client_location_latitude",
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_client_location_longitude",
        ),
        CheckConstraint(
            "accuracy_m > 0 AND accuracy_m <= 10000",
            name="ck_client_location_accuracy",
        ),
        CheckConstraint(
            "precision IS NULL OR precision IN ('PRECISE', 'APPROXIMATE')",
            name="ck_client_location_precision",
        ),
        Index(
            "ix_client_location_human_tenant",
            "human_identity_id",
            "tenant_id",
        ),
    )
