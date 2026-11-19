"""Read-only Google Contacts persistence, isolated from Gmail and authority."""

from sqlalchemy import String, Text, JSON, Float, Integer, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from attention_router.infrastructure.db import Base


class GoogleContactsConnectionRow(Base):
    __tablename__ = "google_contacts_connections"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    envelope: Mapped[dict] = mapped_column(JSON, nullable=False)
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    next_run: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    last_sync: Mapped[float | None] = mapped_column(Float)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(80))


class GoogleContactRow(Base):
    __tablename__ = "google_contacts_cache"
    connection_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("google_contacts_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    resource: Mapped[str] = mapped_column(String(240), primary_key=True)
    etag: Mapped[str | None] = mapped_column(String(512))
    person: Mapped[dict] = mapped_column(JSON, nullable=False)
    search_name: Mapped[str] = mapped_column(Text, nullable=False)
    phones: Mapped[list] = mapped_column(JSON, nullable=False)
    emails: Mapped[list] = mapped_column(JSON, nullable=False)
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
