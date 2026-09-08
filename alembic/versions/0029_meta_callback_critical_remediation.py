"""Add durable callback inbox, bounded failure state and audit convergence."""

from alembic import op
import sqlalchemy as sa


revision = "0029_meta_callback_remediation"
down_revision = "0028_meta_callback_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("operational_state", sa.String(16), nullable=False, server_default="ACTIVE"),
    )
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "meta_delivery_reconciliations", sa.Column("last_failure_code", sa.String(120))
    )
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("quarantined_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("quarantine_reason", sa.String(160)),
    )
    op.create_check_constraint(
        "ck_meta_delivery_reconciliation_operational_state",
        "meta_delivery_reconciliations",
        "operational_state in ('ACTIVE','QUARANTINED')",
    )
    op.create_check_constraint(
        "ck_meta_delivery_reconciliation_failure_count",
        "meta_delivery_reconciliations",
        "failure_count >= 0",
    )
    op.create_check_constraint(
        "ck_meta_delivery_reconciliation_quarantine",
        "meta_delivery_reconciliations",
        "(operational_state = 'ACTIVE' and quarantined_at is null and quarantine_reason is null) "
        "or (operational_state = 'QUARANTINED' and state = 'PENDING' "
        "and quarantined_at is not null and quarantine_reason is not null)",
    )
    op.drop_index(
        "ix_meta_delivery_reconciliation_due",
        table_name="meta_delivery_reconciliations",
    )
    op.create_index(
        "ix_meta_delivery_reconciliation_due",
        "meta_delivery_reconciliations",
        ["next_reconcile_at", "id"],
        postgresql_where=sa.text("state = 'PENDING' AND operational_state = 'ACTIVE'"),
    )
    op.create_index(
        "ix_meta_delivery_reconciliation_quarantined",
        "meta_delivery_reconciliations",
        ["quarantined_at", "id"],
        postgresql_where=sa.text("operational_state = 'QUARANTINED'"),
    )

    op.create_table(
        "meta_callback_inbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider_message_id", sa.String(255), nullable=False),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("provider_status", sa.String(32)),
        sa.Column("provider_timestamp_raw", sa.String(64)),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("errors_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_fingerprint", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column(
            "reconciliation_id",
            sa.String(64),
            sa.ForeignKey("meta_delivery_reconciliations.id"),
        ),
        sa.Column("correlation_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_correlation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(120)),
        sa.Column("correlated_at", sa.DateTime(timezone=True)),
        sa.Column("quarantined_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider_message_id",
            "deduplication_key",
            name="uq_meta_callback_inbox_provider_deduplication",
        ),
        sa.CheckConstraint(
            "state in ('PENDING','CORRELATED','QUARANTINED')",
            name="ck_meta_callback_inbox_state",
        ),
        sa.CheckConstraint(
            "correlation_attempt_count >= 0",
            name="ck_meta_callback_inbox_attempt_count",
        ),
        sa.CheckConstraint(
            "(state = 'PENDING' and reconciliation_id is null and correlated_at is null "
            "and quarantined_at is null) or "
            "(state = 'CORRELATED' and reconciliation_id is not null and correlated_at is not null "
            "and quarantined_at is null) or "
            "(state = 'QUARANTINED' and reconciliation_id is null and correlated_at is null "
            "and quarantined_at is not null)",
            name="ck_meta_callback_inbox_lifecycle",
        ),
    )
    op.create_index(
        "ix_meta_callback_inbox_due",
        "meta_callback_inbox",
        ["next_correlation_at", "id"],
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_index(
        "ix_meta_callback_inbox_provider_state",
        "meta_callback_inbox",
        ["provider_message_id", "state", "id"],
    )

    op.add_column(
        "meta_callback_evidence",
        sa.Column("inbox_id", sa.String(64), sa.ForeignKey("meta_callback_inbox.id")),
    )
    op.create_unique_constraint(
        "uq_meta_callback_evidence_inbox", "meta_callback_evidence", ["inbox_id"]
    )

    op.create_table(
        "meta_callback_audit_markers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "reconciliation_id",
            sa.String(64),
            sa.ForeignKey("meta_delivery_reconciliations.id"),
            nullable=False,
        ),
        sa.Column("marker_type", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "reconciliation_id",
            "marker_type",
            name="uq_meta_callback_audit_marker",
        ),
    )
    op.execute(
        "CREATE TRIGGER meta_callback_audit_marker_immutable "
        "BEFORE UPDATE OR DELETE ON meta_callback_audit_markers FOR EACH ROW "
        "EXECUTE FUNCTION reject_meta_callback_evidence_mutation()"
    )

    op.execute("""
        CREATE FUNCTION enforce_meta_callback_inbox_lifecycle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.provider_message_id IS DISTINCT FROM NEW.provider_message_id
             OR OLD.deduplication_key IS DISTINCT FROM NEW.deduplication_key
             OR OLD.provider_status IS DISTINCT FROM NEW.provider_status
             OR OLD.provider_timestamp_raw IS DISTINCT FROM NEW.provider_timestamp_raw
             OR OLD.provider_timestamp IS DISTINCT FROM NEW.provider_timestamp
             OR OLD.received_at IS DISTINCT FROM NEW.received_at
             OR OLD.valid IS DISTINCT FROM NEW.valid
             OR OLD.errors_present IS DISTINCT FROM NEW.errors_present
             OR OLD.error_fingerprint IS DISTINCT FROM NEW.error_fingerprint
             OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'Meta callback inbox evidence identity is immutable';
          END IF;
          IF OLD.state IN ('CORRELATED','QUARANTINED') AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
            RAISE EXCEPTION 'resolved Meta callback inbox row is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER meta_callback_inbox_lifecycle "
        "BEFORE UPDATE ON meta_callback_inbox FOR EACH ROW "
        "EXECUTE FUNCTION enforce_meta_callback_inbox_lifecycle()"
    )
    op.execute("""
        CREATE FUNCTION enforce_meta_awaiting_delivery_reconciliation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.destination = 'meta_whatsapp_cloud'
             AND NEW.action_type = 'production_conversation_reply'
             AND NEW.status = 'AWAITING_DELIVERY'
             AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
             AND NOT EXISTS (
               SELECT 1 FROM meta_delivery_reconciliations r
               WHERE r.outbox_message_id = NEW.id
                 AND r.provider_message_id = NEW.payload->>'provider_message_id'
             ) THEN
            RAISE EXCEPTION 'accepted Meta attempt requires normalized reconciliation';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER meta_awaiting_delivery_requires_reconciliation "
        "BEFORE INSERT OR UPDATE ON outbox_messages FOR EACH ROW "
        "EXECUTE FUNCTION enforce_meta_awaiting_delivery_reconciliation()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "meta_callback_audit_markers",
        "meta_callback_evidence",
        "meta_callback_inbox",
        "meta_delivery_reconciliations",
    ):
        if table == "meta_callback_evidence":
            condition = "inbox_id IS NOT NULL"
        elif table == "meta_delivery_reconciliations":
            condition = "operational_state <> 'ACTIVE' OR failure_count <> 0"
        else:
            condition = "TRUE"
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {condition}")
        ).scalar_one()
        if count:
            raise RuntimeError("0029 downgrade requires zero remediation state")
    op.execute(
        "DROP TRIGGER IF EXISTS meta_awaiting_delivery_requires_reconciliation ON outbox_messages"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_meta_awaiting_delivery_reconciliation()")
    op.execute("DROP TRIGGER IF EXISTS meta_callback_inbox_lifecycle ON meta_callback_inbox")
    op.execute("DROP FUNCTION IF EXISTS enforce_meta_callback_inbox_lifecycle()")
    op.execute(
        "DROP TRIGGER IF EXISTS meta_callback_audit_marker_immutable "
        "ON meta_callback_audit_markers"
    )
    op.drop_table("meta_callback_audit_markers")
    op.drop_constraint(
        "uq_meta_callback_evidence_inbox", "meta_callback_evidence", type_="unique"
    )
    op.drop_column("meta_callback_evidence", "inbox_id")
    op.drop_index("ix_meta_callback_inbox_provider_state", table_name="meta_callback_inbox")
    op.drop_index("ix_meta_callback_inbox_due", table_name="meta_callback_inbox")
    op.drop_table("meta_callback_inbox")
    op.drop_index(
        "ix_meta_delivery_reconciliation_quarantined",
        table_name="meta_delivery_reconciliations",
    )
    op.drop_index("ix_meta_delivery_reconciliation_due", table_name="meta_delivery_reconciliations")
    op.create_index(
        "ix_meta_delivery_reconciliation_due",
        "meta_delivery_reconciliations",
        ["next_reconcile_at", "id"],
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.drop_constraint(
        "ck_meta_delivery_reconciliation_quarantine",
        "meta_delivery_reconciliations",
        type_="check",
    )
    op.drop_constraint(
        "ck_meta_delivery_reconciliation_failure_count",
        "meta_delivery_reconciliations",
        type_="check",
    )
    op.drop_constraint(
        "ck_meta_delivery_reconciliation_operational_state",
        "meta_delivery_reconciliations",
        type_="check",
    )
    for column in (
        "quarantine_reason",
        "quarantined_at",
        "last_failure_code",
        "last_failure_at",
        "failure_count",
        "operational_state",
    ):
        op.drop_column("meta_delivery_reconciliations", column)
