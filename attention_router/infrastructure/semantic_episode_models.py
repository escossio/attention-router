"""Persistence rows for Personal Context V2D semantic episodes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class SemanticEpisodeRow(Base):
    __tablename__ = "semantic_episodes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_semantic_episode_tenant"),
        nullable=False,
    )
    episode_type: Mapped[str] = mapped_column(String(80), nullable=False)
    semantic_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_episode_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("semantic_episodes.id", name="fk_semantic_episode_supersedes"),
    )
    split_from_episode_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("semantic_episodes.id", name="fk_semantic_episode_split_from"),
    )
    merged_from_episode_ids: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_semantic_episodes"),
        UniqueConstraint(
            "tenant_id",
            "semantic_key",
            name="uq_semantic_episode_tenant_key",
        ),
        CheckConstraint(
            "scope_type IN ('RESOURCE','RELATIONSHIP','THREAD','SEMANTIC_KEY')",
            name="ck_semantic_episode_scope_type",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','CLOSED','SUPERSEDED','SPLIT','MERGED')",
            name="ck_semantic_episode_state",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_semantic_episode_confidence",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_semantic_episode_sensitivity",
        ),
        CheckConstraint(
            "last_activity_at >= started_at",
            name="ck_semantic_episode_activity_order",
        ),
        Index(
            "ix_semantic_episode_scope_activity",
            "tenant_id",
            "episode_type",
            "scope_type",
            "scope_ref",
            "last_activity_at",
        ),
        Index(
            "ix_semantic_episode_tenant_state",
            "tenant_id",
            "state",
            "updated_at",
        ),
    )


class SemanticEpisodeMembershipRow(Base):
    __tablename__ = "semantic_episode_memberships"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_semantic_episode_membership_tenant"),
        nullable=False,
    )
    episode_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("semantic_episodes.id", name="fk_semantic_episode_membership_episode"),
        nullable=False,
    )
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    member_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    association_reason: Mapped[str] = mapped_column(String(48), nullable=False)
    association_source: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    ambiguous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JsonType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_semantic_episode_memberships"),
        UniqueConstraint(
            "episode_id",
            "member_type",
            "member_ref",
            name="uq_semantic_episode_membership_member",
        ),
        CheckConstraint(
            "member_type IN ('TIMELINE_EVENT','CONVERSATION_MESSAGE')",
            name="ck_semantic_episode_membership_type",
        ),
        CheckConstraint(
            "association_reason IN ("
            "'EXACT_RESOURCE',"
            "'EXACT_RELATIONSHIP',"
            "'EXPLICIT_THREAD',"
            "'EXPLICIT_SEMANTIC_KEY'"
            ")",
            name="ck_semantic_episode_membership_reason",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_semantic_episode_membership_confidence",
        ),
        Index(
            "ix_semantic_episode_membership_lookup",
            "tenant_id",
            "member_type",
            "member_ref",
        ),
        Index(
            "ix_semantic_episode_membership_episode",
            "episode_id",
            "observed_at",
        ),
    )


__all__ = [
    "SemanticEpisodeMembershipRow",
    "SemanticEpisodeRow",
]
