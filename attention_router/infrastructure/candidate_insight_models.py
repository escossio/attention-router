"""Persistence rows for Personal Context V2E Candidate Insight."""

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
from attention_router.infrastructure.models import JsonType


class CandidateInsightRow(Base):
    __tablename__ = "candidate_insights"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_candidate_insight_tenant"),
        nullable=False,
    )
    semantic_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    insight_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    predicate: Mapped[str] = mapped_column(String(160), nullable=False)
    proposed_value: Mapped[dict] = mapped_column(JsonType, nullable=False)
    source_engine: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contradiction_refs: Mapped[list[str]] = mapped_column(
        JsonType,
        nullable=False,
        default=list,
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    supersedes_insight_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "candidate_insights.id",
            name="fk_candidate_insight_supersedes",
        ),
    )
    decision_kind: Mapped[str | None] = mapped_column(String(64))
    decision_actor_key: Mapped[str | None] = mapped_column(String(120))
    decision_ref: Mapped[str | None] = mapped_column(String(240))
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_candidate_insights"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_candidate_insight_tenant_idempotency",
        ),
        CheckConstraint(
            "insight_type IN "
            "('CLAIM_PROPOSAL','RELATIONSHIP_PROPOSAL','STATE_PROPOSAL')",
            name="ck_candidate_insight_type",
        ),
        CheckConstraint(
            "subject_type IN "
            "('ACTOR','PERSON','RESOURCE','RELATIONSHIP')",
            name="ck_candidate_insight_subject_type",
        ),
        CheckConstraint(
            "source_engine IN ('RULE','LLM','EMBEDDING','GNN')",
            name="ck_candidate_insight_source_engine",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_insight_confidence",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_candidate_insight_sensitivity",
        ),
        CheckConstraint(
            "state IN "
            "('PROPOSED','ADMITTED','REJECTED','NEEDS_REVIEW','SUPERSEDED')",
            name="ck_candidate_insight_state",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL "
            "OR valid_until >= valid_from",
            name="ck_candidate_insight_temporal_order",
        ),
        Index(
            "ix_candidate_insight_tenant_state",
            "tenant_id",
            "state",
            "updated_at",
        ),
        Index(
            "ix_candidate_insight_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
            "predicate",
        ),
        Index(
            "ix_candidate_insight_semantic_key",
            "tenant_id",
            "semantic_key",
            "updated_at",
        ),
    )


class CandidateInsightEvidenceRow(Base):
    __tablename__ = "candidate_insight_evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "tenants.id",
            name="fk_candidate_insight_evidence_tenant",
        ),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "candidate_insights.id",
            name="fk_candidate_insight_evidence_candidate",
        ),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    independence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_role: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_candidate_insight_evidence"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_candidate_insight_evidence_idempotency",
        ),
        UniqueConstraint(
            "candidate_id",
            "evidence_type",
            "source_ref",
            "evidence_role",
            name="uq_candidate_insight_evidence_source",
        ),
        CheckConstraint(
            "evidence_type IN "
            "('SEMANTIC_EPISODE','TIMELINE_EVENT','CONVERSATION_MESSAGE')",
            name="ck_candidate_insight_evidence_type",
        ),
        CheckConstraint(
            "evidence_role IN ('SUPPORT','CONTRADICTION')",
            name="ck_candidate_insight_evidence_role",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_insight_evidence_confidence",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_candidate_insight_evidence_sensitivity",
        ),
        Index(
            "ix_candidate_insight_evidence_candidate",
            "candidate_id",
            "observed_at",
        ),
        Index(
            "ix_candidate_insight_evidence_source",
            "tenant_id",
            "evidence_type",
            "source_ref",
        ),
    )


__all__ = [
    "CandidateInsightEvidenceRow",
    "CandidateInsightRow",
]
