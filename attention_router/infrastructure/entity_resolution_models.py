"""Persistence rows for Personal Context V2C entity resolution."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import JsonType


class EntityResolutionCandidateRow(Base):
    __tablename__ = "entity_resolution_candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_entity_resolution_candidate_tenant"),
        nullable=False,
    )
    left_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    right_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_kind: Mapped[str | None] = mapped_column(String(40))
    decision_actor_key: Mapped[str | None] = mapped_column(String(120))
    decision_ref: Mapped[str | None] = mapped_column(String(240))
    canonical_actor_key: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_entity_resolution_candidates"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_entity_resolution_candidate_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "left_actor_key",
            "right_actor_key",
            "evidence_fingerprint",
            name="uq_entity_resolution_candidate_pair_evidence",
        ),
        CheckConstraint(
            "left_actor_key < right_actor_key",
            name="ck_entity_resolution_candidate_pair_order",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_entity_resolution_candidate_confidence",
        ),
        CheckConstraint(
            "evidence_count > 0",
            name="ck_entity_resolution_candidate_evidence_count",
        ),
        CheckConstraint(
            "state IN ('PROPOSED','CONFIRMED','REJECTED','AMBIGUOUS','SUPERSEDED')",
            name="ck_entity_resolution_candidate_state",
        ),
        Index(
            "ix_entity_resolution_candidate_tenant_state",
            "tenant_id",
            "state",
            "updated_at",
        ),
    )


class EntityResolutionEvidenceRow(Base):
    __tablename__ = "entity_resolution_evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "entity_resolution_candidates.id",
            name="fk_entity_resolution_evidence_candidate",
        ),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    independence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_entity_resolution_evidence"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_entity_resolution_evidence_idempotency",
        ),
        CheckConstraint(
            "evidence_type IN ("
            "'EXACT_PROVIDER_IDENTITY',"
            "'NORMALIZED_PHONE',"
            "'NORMALIZED_EMAIL',"
            "'OWNER_CONFIRMATION',"
            "'SHARED_STABLE_IDENTIFIER',"
            "'EXPLICIT_RELATIONSHIP',"
            "'SOURCE_ALIAS'"
            ")",
            name="ck_entity_resolution_evidence_type",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_entity_resolution_evidence_confidence",
        ),
        Index(
            "ix_entity_resolution_evidence_candidate",
            "candidate_id",
            "created_at",
        ),
    )


class EntityAliasResolutionRow(Base):
    __tablename__ = "entity_alias_resolutions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_entity_alias_resolution_tenant"),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "entity_resolution_candidates.id",
            name="fk_entity_alias_resolution_candidate",
        ),
        nullable=False,
    )
    alias_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    decision_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    revoked_by_actor_key: Mapped[str | None] = mapped_column(String(120))
    revocation_ref: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_entity_alias_resolutions"),
        UniqueConstraint(
            "candidate_id",
            name="uq_entity_alias_resolution_candidate",
        ),
        CheckConstraint(
            "alias_actor_key <> canonical_actor_key",
            name="ck_entity_alias_resolution_distinct_actor",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','REVOKED','SUPERSEDED')",
            name="ck_entity_alias_resolution_state",
        ),
        Index(
            "uq_entity_alias_resolution_active_alias",
            "tenant_id",
            "alias_actor_key",
            unique=True,
            postgresql_where=text("state='ACTIVE'"),
            sqlite_where=text("state='ACTIVE'"),
        ),
        Index(
            "ix_entity_alias_resolution_canonical",
            "tenant_id",
            "canonical_actor_key",
            "state",
        ),
    )


__all__ = [
    "EntityAliasResolutionRow",
    "EntityResolutionCandidateRow",
    "EntityResolutionEvidenceRow",
]
