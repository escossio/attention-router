"""Add mutable owner operational controls for reply grace."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0032_owner_operational_controls"
down_revision = "0031_owner_reply_grace"
branch_labels = None
depends_on = None


def json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "owner_operational_controls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column("control_key", sa.String(80), nullable=False),
        sa.Column("policy_id", sa.String(80), sa.ForeignKey("policies.identifier"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("integer_value", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by_actor_key", sa.String(120), nullable=False),
        sa.Column("authorization_source", sa.String(80), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("source_event_id", sa.String(180)),
        sa.Column("provenance", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "represented_owner_actor_key", "control_key", "policy_id",
            name="uq_owner_operational_control_scope",
        ),
        sa.CheckConstraint("revision > 0", name="ck_owner_operational_control_revision_positive"),
        sa.CheckConstraint(
            "integer_value >= 0", name="ck_owner_operational_control_integer_nonnegative"
        ),
    )
    op.create_index(
        "ix_owner_operational_control_lookup",
        "owner_operational_controls",
        ["tenant_id", "represented_owner_actor_key", "control_key", "policy_id"],
    )
    op.create_table(
        "owner_operational_control_changes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "control_id", sa.String(64), sa.ForeignKey("owner_operational_controls.id"), nullable=False
        ),
        sa.Column("represented_owner_actor_key", sa.String(120), nullable=False),
        sa.Column("control_key", sa.String(80), nullable=False),
        sa.Column("policy_id", sa.String(80), sa.ForeignKey("policies.identifier"), nullable=False),
        sa.Column("source_channel", sa.String(80), nullable=False),
        sa.Column("source_event_id", sa.String(180)),
        sa.Column("requested_enabled", sa.Boolean()),
        sa.Column("requested_integer_value", sa.Integer()),
        sa.Column("previous_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("provenance", json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "source_channel", "source_event_id",
            name="uq_owner_operational_control_change_event",
        ),
        sa.CheckConstraint(
            "previous_revision >= 0 and resulting_revision > 0",
            name="ck_owner_operational_control_change_revisions",
        ),
        sa.CheckConstraint(
            "requested_integer_value is null or requested_integer_value >= 0",
            name="ck_owner_operational_control_change_integer_nonnegative",
        ),
    )
    op.create_index(
        "ix_owner_operational_control_changes_control",
        "owner_operational_control_changes",
        ["control_id", "created_at"],
    )

    op.add_column(
        "conversation_response_grace_windows",
        sa.Column("represented_owner_actor_key", sa.String(120)),
    )
    op.add_column(
        "conversation_response_grace_windows",
        sa.Column("operational_control_id", sa.String(64)),
    )
    op.add_column(
        "conversation_response_grace_windows",
        sa.Column("operational_control_revision", sa.Integer()),
    )
    op.add_column(
        "conversation_response_grace_windows",
        sa.Column(
            "operational_control_source", sa.String(32), nullable=False,
            server_default="POLICY_DEFAULT",
        ),
    )
    op.add_column(
        "conversation_response_grace_windows",
        sa.Column("auto_release_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "conversation_response_grace_windows",
        sa.Column("effective_grace_seconds", sa.Integer(), nullable=False, server_default="30"),
    )
    op.create_foreign_key(
        "fk_response_grace_operational_control",
        "conversation_response_grace_windows",
        "owner_operational_controls",
        ["operational_control_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_response_grace_control_revision_positive",
        "conversation_response_grace_windows",
        "operational_control_revision is null or operational_control_revision > 0",
    )
    op.create_check_constraint(
        "ck_response_grace_control_source",
        "conversation_response_grace_windows",
        "operational_control_source in ('POLICY_DEFAULT','OWNER_OVERRIDE')",
    )
    op.create_check_constraint(
        "ck_response_grace_effective_seconds_nonnegative",
        "conversation_response_grace_windows",
        "effective_grace_seconds >= 0",
    )
    op.create_index(
        "ix_response_grace_open_owner",
        "conversation_response_grace_windows",
        ["tenant_id", "represented_owner_actor_key", "policy_version_id"],
        postgresql_where=sa.text("state = 'OPEN'"),
    )


def downgrade() -> None:
    op.drop_index("ix_response_grace_open_owner", table_name="conversation_response_grace_windows")
    op.drop_constraint(
        "ck_response_grace_effective_seconds_nonnegative",
        "conversation_response_grace_windows",
        type_="check",
    )
    op.drop_constraint(
        "ck_response_grace_control_source",
        "conversation_response_grace_windows",
        type_="check",
    )
    op.drop_constraint(
        "ck_response_grace_control_revision_positive",
        "conversation_response_grace_windows",
        type_="check",
    )
    op.drop_constraint(
        "fk_response_grace_operational_control",
        "conversation_response_grace_windows",
        type_="foreignkey",
    )
    op.drop_column("conversation_response_grace_windows", "effective_grace_seconds")
    op.drop_column("conversation_response_grace_windows", "auto_release_enabled")
    op.drop_column("conversation_response_grace_windows", "operational_control_source")
    op.drop_column("conversation_response_grace_windows", "operational_control_revision")
    op.drop_column("conversation_response_grace_windows", "operational_control_id")
    op.drop_column("conversation_response_grace_windows", "represented_owner_actor_key")
    op.drop_index(
        "ix_owner_operational_control_changes_control",
        table_name="owner_operational_control_changes",
    )
    op.drop_table("owner_operational_control_changes")
    op.drop_index("ix_owner_operational_control_lookup", table_name="owner_operational_controls")
    op.drop_table("owner_operational_controls")
