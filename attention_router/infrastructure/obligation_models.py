"""Persistence rows for Personal Context V2G obligations and expectations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class RecurringObligationDefinitionRow(Base):
    __tablename__ = "recurring_obligation_definitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_obligation_definition_tenant"),
        nullable=False,
    )
    semantic_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    obligation_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    value_constraints: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    cadence_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    due_day: Mapped[int] = mapped_column(Integer, nullable=False)
    due_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(160))
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    supersedes_definition_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "recurring_obligation_definitions.id",
            name="fk_obligation_definition_supersedes",
        ),
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_recurring_obligation_definitions"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_obligation_definition_tenant_idempotency",
        ),
        CheckConstraint(
            "subject_type IN ('RESOURCE','RELATIONSHIP')",
            name="ck_obligation_definition_subject_type",
        ),
        CheckConstraint(
            "cadence_kind = 'MONTHLY'",
            name="ck_obligation_definition_cadence",
        ),
        CheckConstraint(
            "due_day >= 1 AND due_day <= 28",
            name="ck_obligation_definition_due_day",
        ),
        CheckConstraint(
            "grace_seconds >= 0 AND grace_seconds <= 2592000",
            name="ck_obligation_definition_grace",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_obligation_definition_confidence",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_obligation_definition_sensitivity",
        ),
        CheckConstraint(
            "source_kind IN ('OWNER_DECLARED','ADMITTED_CANDIDATE')",
            name="ck_obligation_definition_source_kind",
        ),
        CheckConstraint(
            "state IN ('ACTIVE','SUPERSEDED','REVOKED')",
            name="ck_obligation_definition_state",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until >= valid_from",
            name="ck_obligation_definition_temporal_order",
        ),
        Index(
            "uq_obligation_definition_active_semantic",
            "tenant_id",
            "semantic_key",
            unique=True,
            postgresql_where=text("state='ACTIVE'"),
            sqlite_where=text("state='ACTIVE'"),
        ),
        Index(
            "ix_obligation_definition_tenant_state",
            "tenant_id",
            "state",
            "updated_at",
        ),
        Index(
            "ix_obligation_definition_subject",
            "tenant_id",
            "subject_type",
            "subject_id",
            "state",
        ),
        Index(
            "ix_obligation_definition_expected_actor",
            "tenant_id",
            "expected_actor_key",
            "state",
        ),
    )


class ObligationInstanceRow(Base):
    __tablename__ = "obligation_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_obligation_instance_tenant"),
        nullable=False,
    )
    definition_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "recurring_obligation_definitions.id",
            name="fk_obligation_instance_definition",
        ),
        nullable=False,
    )
    period_key: Mapped[str] = mapped_column(String(7), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_by: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    uncertainty_code: Mapped[str | None] = mapped_column(String(64))
    expected_value: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    satisfaction_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    extension_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_instance_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "obligation_instances.id",
            name="fk_obligation_instance_supersedes",
        ),
    )
    provenance: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_obligation_instances"),
        UniqueConstraint(
            "definition_id",
            "period_key",
            name="uq_obligation_instance_definition_period",
        ),
        CheckConstraint(
            "state IN ("
            "'EXPECTED','SATISFIED','PARTIALLY_SATISFIED','EXTENDED',"
            "'WAIVED','UNCONFIRMED_AFTER_DUE','SUPERSEDED'"
            ")",
            name="ck_obligation_instance_state",
        ),
        CheckConstraint(
            "reconciliation_status IN ("
            "'PENDING','PARTIAL','CONFIRMED','EXTENDED','WAIVED',"
            "'UNCONFIRMED','LATE_CONFIRMED','SUPERSEDED'"
            ")",
            name="ck_obligation_instance_reconciliation",
        ),
        CheckConstraint(
            "satisfaction_ratio >= 0 AND satisfaction_ratio <= 1",
            name="ck_obligation_instance_satisfaction",
        ),
        CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_obligation_instance_sensitivity",
        ),
        CheckConstraint(
            "period_end > period_start",
            name="ck_obligation_instance_period_order",
        ),
        CheckConstraint(
            "due_window_end >= expected_by",
            name="ck_obligation_instance_due_window_order",
        ),
        Index(
            "ix_obligation_instance_tenant_state",
            "tenant_id",
            "state",
            "expected_by",
        ),
        Index(
            "ix_obligation_instance_definition_period",
            "definition_id",
            "period_start",
            "period_end",
        ),
    )


class ObligationFulfillmentRow(Base):
    __tablename__ = "obligation_fulfillments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_obligation_fulfillment_tenant"),
        nullable=False,
    )
    instance_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("obligation_instances.id", name="fk_obligation_fulfillment_instance"),
        nullable=False,
    )
    timeline_event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("timeline_events.id", name="fk_obligation_fulfillment_event"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fulfillment_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    observed_value: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    explicitly_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconciliation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_obligation_fulfillments"),
        UniqueConstraint(
            "instance_id",
            "timeline_event_id",
            name="uq_obligation_fulfillment_instance_event",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_obligation_fulfillment_idempotency",
        ),
        CheckConstraint(
            "fulfillment_fraction > 0 AND fulfillment_fraction <= 1",
            name="ck_obligation_fulfillment_fraction",
        ),
        CheckConstraint(
            "reconciliation_kind IN ("
            "'ON_TIME','LATE_INGESTED','LATE_OBSERVED','EXPLICIT_SHARED'"
            ")",
            name="ck_obligation_fulfillment_kind",
        ),
        Index(
            "ix_obligation_fulfillment_event",
            "tenant_id",
            "timeline_event_id",
        ),
        Index(
            "ix_obligation_fulfillment_instance",
            "instance_id",
            "created_at",
        ),
    )


class ObligationTransitionRow(Base):
    __tablename__ = "obligation_transitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.id", name="fk_obligation_transition_tenant"),
        nullable=False,
    )
    instance_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("obligation_instances.id", name="fk_obligation_transition_instance"),
        nullable=False,
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_actor_key: Mapped[str | None] = mapped_column(String(120))
    decision_ref: Mapped[str | None] = mapped_column(String(240))
    evidence_ref: Mapped[str | None] = mapped_column(String(160))
    metadata_json: Mapped[dict] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_obligation_transitions"),
        CheckConstraint(
            "to_state IN ("
            "'EXPECTED','SATISFIED','PARTIALLY_SATISFIED','EXTENDED',"
            "'WAIVED','UNCONFIRMED_AFTER_DUE','SUPERSEDED'"
            ")",
            name="ck_obligation_transition_to_state",
        ),
        Index(
            "ix_obligation_transition_instance",
            "instance_id",
            "created_at",
        ),
    )


__all__ = [
    "ObligationFulfillmentRow",
    "ObligationInstanceRow",
    "ObligationTransitionRow",
    "RecurringObligationDefinitionRow",
]
