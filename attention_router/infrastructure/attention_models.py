"""Persistence rows for Personal Context V2H attention assessments."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import JsonType


class AttentionAssessmentRow(Base):
    __tablename__ = "attention_assessments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_attention_assessment_tenant"),
        nullable=False,
    )
    signal_key: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    source_state: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_class: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_class: Mapped[str] = mapped_column(String(40), nullable=False)
    components: Mapped[dict] = mapped_column(JsonType, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_suppressed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    owner_suppression_reason: Mapped[str | None] = mapped_column(String(64))
    owner_suppressed_by: Mapped[str | None] = mapped_column(String(120))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    owner_decision_ref: Mapped[str | None] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    supersedes_assessment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "attention_assessments.id",
            name="fk_attention_assessment_supersedes",
        ),
    )
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_attention_assessments"),
        UniqueConstraint(
            "tenant_id",
            "signal_key",
            "snapshot_fingerprint",
            name="uq_attention_assessment_snapshot",
        ),
        CheckConstraint(
            "source_type IN ('OBLIGATION_INSTANCE','MEMORY_CLAIM_ANOMALY')",
            name="ck_attention_assessment_source_type",
        ),
        CheckConstraint(
            "score >= 0 AND score <= 1",
            name="ck_attention_assessment_score",
        ),
        CheckConstraint(
            "score_class IN ("
            "'IGNORE','REASONING_QUEUE','OWNER_SUGGESTION_CANDIDATE'"
            ")",
            name="ck_attention_assessment_score_class",
        ),
        CheckConstraint(
            "effective_class IN ("
            "'IGNORE','REASONING_QUEUE','OWNER_SUGGESTION_CANDIDATE'"
            ")",
            name="ck_attention_assessment_effective_class",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE')",
            name="ck_attention_assessment_sensitivity",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','ACKNOWLEDGED','SUPPRESSED','SUPERSEDED')",
            name="ck_attention_assessment_status",
        ),
        Index(
            "uq_attention_assessment_active_signal",
            "tenant_id",
            "signal_key",
            unique=True,
            postgresql_where=text("status='ACTIVE'"),
            sqlite_where=text("status='ACTIVE'"),
        ),
        Index(
            "ix_attention_assessment_tenant_class",
            "tenant_id",
            "effective_class",
            "status",
            "updated_at",
        ),
        Index(
            "ix_attention_assessment_source",
            "tenant_id",
            "source_type",
            "source_ref",
        ),
    )


__all__ = ["AttentionAssessmentRow"]
