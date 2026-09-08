"""Add normalized Meta callback reconciliation inbox and schedule."""

from alembic import op
import sqlalchemy as sa


revision = "0028_meta_callback_reconcile"
down_revision = "0027_frozen_retirement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 0027 did not persist the durable API_ACCEPTED instant outside audit JSON.
        # Refuse an ambiguous upgrade before creating any V2 object instead of
        # inventing timing/authority state or leaving an accepted attempt orphaned.
        bind.execute(
            sa.text("LOCK TABLE outbox_messages IN SHARE ROW EXCLUSIVE MODE")
        )
    in_flight = bind.execute(
        sa.text(
            "SELECT count(*) FROM outbox_messages "
            "WHERE destination = 'meta_whatsapp_cloud' "
            "AND action_type = 'production_conversation_reply' "
            "AND status = 'AWAITING_DELIVERY'"
        )
    ).scalar_one()
    if in_flight:
        raise RuntimeError(
            "0028 upgrade requires zero in-flight accepted Meta delivery attempts"
        )
    op.create_table(
        "meta_delivery_reconciliations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "outbox_message_id",
            sa.String(64),
            sa.ForeignKey("outbox_messages.id"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(255), nullable=False),
        sa.Column("api_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closure_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("terminal_reason", sa.String(120)),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "outbox_message_id", name="uq_meta_delivery_reconciliation_outbox"
        ),
        sa.UniqueConstraint(
            "provider_message_id", name="uq_meta_delivery_reconciliation_provider"
        ),
        sa.CheckConstraint(
            "state in ('PENDING','PASSED','FAILED')",
            name="ck_meta_delivery_reconciliation_state",
        ),
        sa.CheckConstraint(
            "evidence_count >= 0",
            name="ck_meta_delivery_reconciliation_evidence_count",
        ),
        sa.CheckConstraint(
            "deadline_at > api_accepted_at and closure_after > deadline_at",
            name="ck_meta_delivery_reconciliation_windows",
        ),
        sa.CheckConstraint(
            "(state = 'PENDING' and terminal_reason is null and terminal_at is null) "
            "or (state in ('PASSED','FAILED') and terminal_reason is not null "
            "and terminal_at is not null)",
            name="ck_meta_delivery_reconciliation_terminal",
        ),
    )
    op.create_index(
        "ix_meta_delivery_reconciliation_due",
        "meta_delivery_reconciliations",
        ["next_reconcile_at", "id"],
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_table(
        "meta_callback_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "reconciliation_id",
            sa.String(64),
            sa.ForeignKey("meta_delivery_reconciliations.id"),
            nullable=False,
        ),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("provider_status", sa.String(32)),
        sa.Column("provider_timestamp_raw", sa.String(64)),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("admissible", sa.Boolean(), nullable=False),
        sa.Column("errors_present", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_fingerprint", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "reconciliation_id",
            "deduplication_key",
            name="uq_meta_callback_evidence_deduplication",
        ),
    )
    op.create_index(
        "ix_meta_callback_evidence_reconciliation_received",
        "meta_callback_evidence",
        ["reconciliation_id", "received_at", "id"],
    )
    op.execute("""
        CREATE FUNCTION reject_meta_callback_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'Meta callback evidence is append-only';
        END $$
    """)
    op.execute(
        "CREATE TRIGGER meta_callback_evidence_immutable "
        "BEFORE UPDATE OR DELETE ON meta_callback_evidence FOR EACH ROW "
        "EXECUTE FUNCTION reject_meta_callback_evidence_mutation()"
    )
    op.execute("""
        CREATE FUNCTION enforce_meta_delivery_reconciliation_lifecycle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.outbox_message_id IS DISTINCT FROM NEW.outbox_message_id
             OR OLD.provider_message_id IS DISTINCT FROM NEW.provider_message_id
             OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
             OR OLD.api_accepted_at IS DISTINCT FROM NEW.api_accepted_at
             OR OLD.deadline_at IS DISTINCT FROM NEW.deadline_at
             OR OLD.closure_after IS DISTINCT FROM NEW.closure_after THEN
            RAISE EXCEPTION 'Meta reconciliation identity and windows are immutable';
          END IF;
          IF OLD.state IN ('PASSED', 'FAILED') AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
            RAISE EXCEPTION 'terminal Meta reconciliation is immutable';
          END IF;
          IF OLD.state = 'PENDING' AND NEW.state NOT IN ('PENDING', 'PASSED', 'FAILED') THEN
            RAISE EXCEPTION 'invalid Meta reconciliation lifecycle transition';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER meta_delivery_reconciliation_lifecycle "
        "BEFORE UPDATE ON meta_delivery_reconciliations FOR EACH ROW "
        "EXECUTE FUNCTION enforce_meta_delivery_reconciliation_lifecycle()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM meta_callback_evidence")).scalar_one():
        raise RuntimeError("0028 downgrade requires zero Meta callback evidence")
    if bind.execute(
        sa.text("SELECT count(*) FROM meta_delivery_reconciliations")
    ).scalar_one():
        raise RuntimeError("0028 downgrade requires zero Meta delivery reconciliations")
    op.execute(
        "DROP TRIGGER IF EXISTS meta_delivery_reconciliation_lifecycle "
        "ON meta_delivery_reconciliations"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_meta_delivery_reconciliation_lifecycle()")
    op.execute("DROP TRIGGER IF EXISTS meta_callback_evidence_immutable ON meta_callback_evidence")
    op.execute("DROP FUNCTION IF EXISTS reject_meta_callback_evidence_mutation()")
    op.drop_index(
        "ix_meta_callback_evidence_reconciliation_received",
        table_name="meta_callback_evidence",
    )
    op.drop_table("meta_callback_evidence")
    op.drop_index(
        "ix_meta_delivery_reconciliation_due",
        table_name="meta_delivery_reconciliations",
    )
    op.drop_table("meta_delivery_reconciliations")
