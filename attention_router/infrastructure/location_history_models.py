"""Append-only owner/device scoped location observations."""

from datetime import datetime
from sqlalchemy import String, Float, DateTime, ForeignKey, CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from attention_router.infrastructure.db import Base


class LocationObservationRow(Base):
    __tablename__ = "client_location_observations"
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("client_devices.id"), primary_key=True
    )
    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    human_identity_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("human_identities.id"), nullable=False
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float] = mapped_column(Float, nullable=False)
    precision: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_history_latitude"),
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_history_longitude"),
        CheckConstraint("accuracy_m > 0 AND accuracy_m <= 10000", name="ck_history_accuracy"),
        CheckConstraint("precision IN ('PRECISE','APPROXIMATE')", name="ck_history_precision"),
        Index(
            "ix_location_history_chronology",
            "tenant_id",
            "human_identity_id",
            "device_id",
            "captured_at",
        ),
    )
