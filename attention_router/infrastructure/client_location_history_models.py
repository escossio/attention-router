"""Immutable observations of the device's position at a specific instant.

No human-presence or WhatsApp-sending-device assertion is implied.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


class ClientLocationObservationRow(Base):
    __tablename__ = "client_location_observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False
    )
    human_identity_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("human_identities.id"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("client_devices.id"), nullable=False
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float] = mapped_column(Float, nullable=False)
    precision: Mapped[str | None] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "device_id", "captured_at",
            name="uq_client_location_observation_device_time",
        ),
        CheckConstraint(
            "latitude >= -90 and latitude <= 90",
            name="ck_client_location_observation_lat",
        ),
        CheckConstraint(
            "longitude >= -180 and longitude <= 180",
            name="ck_client_location_observation_lon",
        ),
        CheckConstraint(
            "accuracy_m > 0 and accuracy_m <= 10000",
            name="ck_client_location_observation_accuracy",
        ),
        CheckConstraint(
            "precision is null or precision in ('PRECISE','APPROXIMATE')",
            name="ck_client_location_observation_precision",
        ),
        Index(
            "ix_client_location_observation_owner_time",
            "tenant_id", "human_identity_id", "captured_at",
        ),
        Index(
            "ix_client_location_observation_device_time",
            "tenant_id", "device_id", "captured_at",
        ),
    )
