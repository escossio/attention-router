"""Operational presentation receipts, independent of memory and actor authority."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer,
    String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base


STATES_SQL = "'RESERVED','QUEUED','SENDING','SENT','CONFIRMED','FAILED','AMBIGUOUS'"


class FirstContactPresentationRow(Base):
    __tablename__ = "first_contact_presentations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False)
    canonical_peer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    presentation_version: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    current_attempt_id: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "channel", "source_account", "canonical_peer_hash",
            name="uq_first_contact_scope",
        ),
        ForeignKeyConstraint(
            ["id", "current_attempt_id"],
            ["first_contact_attempts.presentation_id", "first_contact_attempts.id"],
            name="fk_first_contact_current_attempt_scope", use_alter=True,
        ),
        CheckConstraint(f"state IN ({STATES_SQL})", name="ck_first_contact_state"),
        CheckConstraint("generation > 0 AND revision >= 0", name="ck_first_contact_revision"),
        CheckConstraint("length(canonical_peer_hash) = 64", name="ck_first_contact_peer_hash"),
        CheckConstraint(
            "(state = 'CONFIRMED') = (confirmed_at IS NOT NULL)",
            name="ck_first_contact_confirmation",
        ),
        Index("ix_first_contact_state", "tenant_id", "state"),
    )


class FirstContactAttemptRow(Base):
    __tablename__ = "first_contact_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    presentation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("first_contact_presentations.id"), nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    presentation_version: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_decisions.id"))
    execution_intent_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_execution_intents.id"),
    )
    outbox_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("outbox_messages.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(180))
    text_hash: Mapped[str | None] = mapped_column(String(64))
    message_reference: Mapped[str | None] = mapped_column(String(512))
    ack: Mapped[int | None] = mapped_column(Integer)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proof_sha256: Mapped[str | None] = mapped_column(String(64))
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("presentation_id", "id", name="uq_first_contact_attempt_scope_id"),
        UniqueConstraint("presentation_id", "generation", name="uq_first_contact_generation"),
        UniqueConstraint("event_id", name="uq_first_contact_attempt_event"),
        UniqueConstraint("decision_id", name="uq_first_contact_attempt_decision"),
        UniqueConstraint("execution_intent_id", name="uq_first_contact_attempt_intent"),
        UniqueConstraint("outbox_id", name="uq_first_contact_attempt_outbox"),
        CheckConstraint(f"state IN ({STATES_SQL})", name="ck_first_contact_attempt_state"),
        CheckConstraint("generation > 0", name="ck_first_contact_attempt_generation"),
        CheckConstraint("text_hash IS NULL OR length(text_hash) = 64", name="ck_first_contact_text_hash"),
        CheckConstraint("ack IS NULL OR ack BETWEEN 0 AND 4", name="ck_first_contact_ack"),
        CheckConstraint(
            "state != 'CONFIRMED' OR (confirmed_at IS NOT NULL AND ack IS NOT NULL AND ack >= 2 "
            "AND ack_at IS NOT NULL AND decision_id IS NOT NULL AND execution_intent_id IS NOT NULL "
            "AND idempotency_key IS NOT NULL "
            "AND proof_sha256 IS NOT NULL AND message_reference IS NOT NULL "
            "AND outbox_id IS NOT NULL AND text_hash IS NOT NULL)",
            name="ck_first_contact_attempt_confirmation",
        ),
    )
