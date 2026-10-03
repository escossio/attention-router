"""Personal Context V2G obligation and expectation model.

Revision ID: 0055_obligation_expectation_v0
Revises: 0054_candidate_insight_v0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0055_obligation_expectation_v0"
down_revision = "0054_candidate_insight_v0"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "recurring_obligation_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_obligation_definition_tenant"),
            nullable=False,
        ),
        sa.Column("semantic_key", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("obligation_kind", sa.String(80), nullable=False),
        sa.Column("expected_actor_key", sa.String(120), nullable=False),
        sa.Column("expected_event_type", sa.String(80), nullable=False),
        sa.Column("value_constraints", json_type, nullable=False),
        sa.Column("cadence_kind", sa.String(24), nullable=False),
        sa.Column("due_day", sa.Integer(), nullable=False),
        sa.Column("due_timezone", sa.String(64), nullable=False),
        sa.Column("grace_seconds", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=True),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column(
            "supersedes_definition_id",
            sa.String(64),
            sa.ForeignKey(
                "recurring_obligation_definitions.id",
                name="fk_obligation_definition_supersedes",
            ),
            nullable=True,
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_obligation_definition_tenant_idempotency",
        ),
        sa.CheckConstraint(
            "subject_type IN ('RESOURCE','RELATIONSHIP')",
            name="ck_obligation_definition_subject_type",
        ),
        sa.CheckConstraint(
            "cadence_kind = 'MONTHLY'",
            name="ck_obligation_definition_cadence",
        ),
        sa.CheckConstraint(
            "due_day >= 1 AND due_day <= 28",
            name="ck_obligation_definition_due_day",
        ),
        sa.CheckConstraint(
            "grace_seconds >= 0 AND grace_seconds <= 2592000",
            name="ck_obligation_definition_grace",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_obligation_definition_confidence",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_obligation_definition_sensitivity",
        ),
        sa.CheckConstraint(
            "source_kind IN ('OWNER_DECLARED','ADMITTED_CANDIDATE')",
            name="ck_obligation_definition_source_kind",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','SUPERSEDED','REVOKED')",
            name="ck_obligation_definition_state",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until >= valid_from",
            name="ck_obligation_definition_temporal_order",
        ),
    )
    op.create_index(
        "uq_obligation_definition_active_semantic",
        "recurring_obligation_definitions",
        ["tenant_id", "semantic_key"],
        unique=True,
        postgresql_where=sa.text("state='ACTIVE'"),
        sqlite_where=sa.text("state='ACTIVE'"),
    )
    op.create_index(
        "ix_obligation_definition_tenant_state",
        "recurring_obligation_definitions",
        ["tenant_id", "state", "updated_at"],
    )
    op.create_index(
        "ix_obligation_definition_subject",
        "recurring_obligation_definitions",
        ["tenant_id", "subject_type", "subject_id", "state"],
    )
    op.create_index(
        "ix_obligation_definition_expected_actor",
        "recurring_obligation_definitions",
        ["tenant_id", "expected_actor_key", "state"],
    )

    op.create_table(
        "obligation_instances",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_obligation_instance_tenant"),
            nullable=False,
        ),
        sa.Column(
            "definition_id",
            sa.String(64),
            sa.ForeignKey(
                "recurring_obligation_definitions.id",
                name="fk_obligation_instance_definition",
            ),
            nullable=False,
        ),
        sa.Column("period_key", sa.String(7), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_by", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reconciliation_status", sa.String(40), nullable=False),
        sa.Column("uncertainty_code", sa.String(64), nullable=True),
        sa.Column("expected_value", json_type, nullable=False),
        sa.Column("satisfaction_ratio", sa.Float(), nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("extension_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_instance_id",
            sa.String(64),
            sa.ForeignKey(
                "obligation_instances.id",
                name="fk_obligation_instance_supersedes",
            ),
            nullable=True,
        ),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "definition_id",
            "period_key",
            name="uq_obligation_instance_definition_period",
        ),
        sa.CheckConstraint(
            "state IN ("
            "'EXPECTED','SATISFIED','PARTIALLY_SATISFIED','EXTENDED',"
            "'WAIVED','UNCONFIRMED_AFTER_DUE','SUPERSEDED'"
            ")",
            name="ck_obligation_instance_state",
        ),
        sa.CheckConstraint(
            "reconciliation_status IN ("
            "'PENDING','PARTIAL','CONFIRMED','EXTENDED','WAIVED',"
            "'UNCONFIRMED','LATE_CONFIRMED','SUPERSEDED'"
            ")",
            name="ck_obligation_instance_reconciliation",
        ),
        sa.CheckConstraint(
            "satisfaction_ratio >= 0 AND satisfaction_ratio <= 1",
            name="ck_obligation_instance_satisfaction",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_obligation_instance_sensitivity",
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_obligation_instance_period_order",
        ),
        sa.CheckConstraint(
            "due_window_end >= expected_by",
            name="ck_obligation_instance_due_window_order",
        ),
    )
    op.create_index(
        "ix_obligation_instance_tenant_state",
        "obligation_instances",
        ["tenant_id", "state", "expected_by"],
    )
    op.create_index(
        "ix_obligation_instance_definition_period",
        "obligation_instances",
        ["definition_id", "period_start", "period_end"],
    )

    op.create_table(
        "obligation_fulfillments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_obligation_fulfillment_tenant"),
            nullable=False,
        ),
        sa.Column(
            "instance_id",
            sa.String(64),
            sa.ForeignKey(
                "obligation_instances.id",
                name="fk_obligation_fulfillment_instance",
            ),
            nullable=False,
        ),
        sa.Column(
            "timeline_event_id",
            sa.String(64),
            sa.ForeignKey(
                "timeline_events.id",
                name="fk_obligation_fulfillment_event",
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("fulfillment_fraction", sa.Float(), nullable=False),
        sa.Column("observed_value", json_type, nullable=False),
        sa.Column("explicitly_shared", sa.Boolean(), nullable=False),
        sa.Column("reconciliation_kind", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "instance_id",
            "timeline_event_id",
            name="uq_obligation_fulfillment_instance_event",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_obligation_fulfillment_idempotency",
        ),
        sa.CheckConstraint(
            "fulfillment_fraction > 0 AND fulfillment_fraction <= 1",
            name="ck_obligation_fulfillment_fraction",
        ),
        sa.CheckConstraint(
            "reconciliation_kind IN ("
            "'ON_TIME','LATE_INGESTED','LATE_OBSERVED','EXPLICIT_SHARED'"
            ")",
            name="ck_obligation_fulfillment_kind",
        ),
    )
    op.create_index(
        "ix_obligation_fulfillment_event",
        "obligation_fulfillments",
        ["tenant_id", "timeline_event_id"],
    )
    op.create_index(
        "ix_obligation_fulfillment_instance",
        "obligation_fulfillments",
        ["instance_id", "created_at"],
    )

    op.create_table(
        "obligation_transitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_obligation_transition_tenant"),
            nullable=False,
        ),
        sa.Column(
            "instance_id",
            sa.String(64),
            sa.ForeignKey(
                "obligation_instances.id",
                name="fk_obligation_transition_instance",
            ),
            nullable=False,
        ),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("decision_actor_key", sa.String(120), nullable=True),
        sa.Column("decision_ref", sa.String(240), nullable=True),
        sa.Column("evidence_ref", sa.String(160), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "to_state IN ("
            "'EXPECTED','SATISFIED','PARTIALLY_SATISFIED','EXTENDED',"
            "'WAIVED','UNCONFIRMED_AFTER_DUE','SUPERSEDED'"
            ")",
            name="ck_obligation_transition_to_state",
        ),
    )
    op.create_index(
        "ix_obligation_transition_instance",
        "obligation_transitions",
        ["instance_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "obligation_transitions",
        "obligation_fulfillments",
        "obligation_instances",
        "recurring_obligation_definitions",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "OBLIGATION_EXPECTATION_DOWNGRADE_REQUIRES_DATA_EXPORT"
            )

    op.drop_index(
        "ix_obligation_transition_instance",
        table_name="obligation_transitions",
    )
    op.drop_table("obligation_transitions")

    op.drop_index(
        "ix_obligation_fulfillment_instance",
        table_name="obligation_fulfillments",
    )
    op.drop_index(
        "ix_obligation_fulfillment_event",
        table_name="obligation_fulfillments",
    )
    op.drop_table("obligation_fulfillments")

    op.drop_index(
        "ix_obligation_instance_definition_period",
        table_name="obligation_instances",
    )
    op.drop_index(
        "ix_obligation_instance_tenant_state",
        table_name="obligation_instances",
    )
    op.drop_table("obligation_instances")

    op.drop_index(
        "ix_obligation_definition_expected_actor",
        table_name="recurring_obligation_definitions",
    )
    op.drop_index(
        "ix_obligation_definition_subject",
        table_name="recurring_obligation_definitions",
    )
    op.drop_index(
        "ix_obligation_definition_tenant_state",
        table_name="recurring_obligation_definitions",
    )
    op.drop_index(
        "uq_obligation_definition_active_semantic",
        table_name="recurring_obligation_definitions",
    )
    op.drop_table("recurring_obligation_definitions")
