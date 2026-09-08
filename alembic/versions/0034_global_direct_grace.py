"""Global Grace scope and unbound direct conversations; preserve values/history."""

from alembic import op
import sqlalchemy as sa

revision = "0034_global_direct_grace"
down_revision = "0033_owner_automation_controls"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    ambiguous = bind.execute(
        sa.text("""
        SELECT 1 FROM owner_operational_controls WHERE control_key='owner_reply_grace'
        GROUP BY tenant_id, represented_owner_actor_key HAVING count(*) > 1
    """)
    ).first()
    if ambiguous:
        raise RuntimeError("GLOBAL_GRACE_MIGRATION_AMBIGUOUS")
    op.add_column(
        "owner_operational_controls",
        sa.Column("scope_type", sa.String(16), nullable=False, server_default="POLICY"),
    )
    op.alter_column("owner_operational_controls", "policy_id", nullable=True)
    op.alter_column("owner_operational_control_changes", "policy_id", nullable=True)
    op.alter_column("conversation_response_grace_windows", "actor_binding_id", nullable=True)
    op.drop_constraint("ck_response_grace_control_source", "conversation_response_grace_windows")
    op.create_check_constraint(
        "ck_response_grace_control_source",
        "conversation_response_grace_windows",
        "operational_control_source in ('POLICY_DEFAULT','GLOBAL_DEFAULT','OWNER_OVERRIDE')",
    )
    op.add_column("autonomy_evaluations", sa.Column("conversation_contract_version", sa.String(32)))
    bind.execute(
        sa.text("""
        UPDATE owner_operational_controls
        SET provenance=provenance || jsonb_build_object('global_grace_migration',
            jsonb_build_object('revision','0034_global_direct_grace','legacy_policy_id',policy_id)),
            scope_type='GLOBAL', policy_id=NULL
        WHERE control_key='owner_reply_grace'
    """)
    )
    op.create_check_constraint(
        "ck_owner_operational_scope",
        "owner_operational_controls",
        "(scope_type='GLOBAL' AND policy_id IS NULL) OR (scope_type='POLICY' AND policy_id IS NOT NULL)",
    )
    op.create_index(
        "uq_owner_operational_global",
        "owner_operational_controls",
        ["tenant_id", "represented_owner_actor_key", "control_key"],
        unique=True,
        postgresql_where=sa.text("scope_type='GLOBAL'"),
    )
    # Existing unique(policy_id, owner, tenant, key) remains the POLICY constraint.


def downgrade():
    bind = op.get_bind()
    # Never fabricate a policy/binding, or destroy new data to make downgrade fit.
    if bind.execute(
        sa.text("""
        SELECT 1 FROM owner_operational_controls WHERE scope_type='GLOBAL'
        AND provenance->'global_grace_migration'->>'legacy_policy_id' IS NULL
        UNION ALL SELECT 1 FROM conversation_response_grace_windows
        WHERE actor_binding_id IS NULL OR provenance->>'grace_contract_version' IS NOT NULL
        UNION ALL SELECT 1 FROM owner_operational_control_changes WHERE policy_id IS NULL
    """)
    ).first():
        raise RuntimeError("GLOBAL_GRACE_DOWNGRADE_REQUIRES_DATA_MIGRATION")
    op.drop_constraint("ck_owner_operational_scope", "owner_operational_controls")
    op.drop_index("uq_owner_operational_global", "owner_operational_controls")
    bind.execute(
        sa.text("""
        UPDATE owner_operational_controls
        SET policy_id=provenance->'global_grace_migration'->>'legacy_policy_id', scope_type='POLICY'
        WHERE scope_type='GLOBAL'
    """)
    )
    op.alter_column("owner_operational_controls", "policy_id", nullable=False)
    op.alter_column("owner_operational_control_changes", "policy_id", nullable=False)
    op.alter_column("conversation_response_grace_windows", "actor_binding_id", nullable=False)
    op.drop_constraint("ck_response_grace_control_source", "conversation_response_grace_windows")
    op.create_check_constraint(
        "ck_response_grace_control_source",
        "conversation_response_grace_windows",
        "operational_control_source in ('POLICY_DEFAULT','OWNER_OVERRIDE')",
    )
    op.drop_column("owner_operational_controls", "scope_type")
    op.drop_column("autonomy_evaluations", "conversation_contract_version")
