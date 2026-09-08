"""Add versioned production authority scope primitives."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_production_authority_scope"
down_revision = "0025_human_auth_execution_intent"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "execution_class_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("identity", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("environment_classification", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("allowed_transports", JSONB, nullable=False),
        sa.Column("allowed_operations", JSONB, nullable=False),
        sa.Column("allowed_capabilities", JSONB, nullable=False),
        sa.Column("external_effect_class", sa.String(80), nullable=False),
        sa.Column("max_target_cardinality", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requires_human_authorization", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_fresh_readiness", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_handoff", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_bounded_authorization", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("direct_execution_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("identity", "version", name="uq_execution_class_version_identity"),
        sa.CheckConstraint("version > 0", name="ck_execution_class_version_positive"),
        sa.CheckConstraint("max_target_cardinality >= 0", name="ck_execution_class_target_cardinality"),
        sa.CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_execution_class_environment"),
        sa.CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_execution_class_status"),
    )
    op.create_table(
        "safety_set_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("identity", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("environment_classification", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("execution_class_version_id", sa.String(64), sa.ForeignKey("execution_class_versions.id"), nullable=False),
        sa.Column("allowed_transport", sa.String(80), nullable=False),
        sa.Column("allowed_operation", sa.String(120), nullable=False),
        sa.Column("allowed_capability", sa.String(120), nullable=False),
        sa.Column("max_target_cardinality", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_outbound_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_action_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required_invariants", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("identity", "version", name="uq_safety_set_version_identity"),
        sa.CheckConstraint("version > 0", name="ck_safety_set_version_positive"),
        sa.CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_safety_set_environment"),
        sa.CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_safety_set_status"),
        sa.CheckConstraint("max_target_cardinality >= 0 and max_outbound_messages >= 0 and max_action_count >= 0", name="ck_safety_set_ceilings"),
    )
    op.create_table(
        "static_intent_authority_profiles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("identity", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("environment_classification", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("max_target_cardinality", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_outbound_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_action_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_transport", sa.String(80), nullable=False),
        sa.Column("allowed_operation", sa.String(120), nullable=False),
        sa.Column("allowed_capability", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("identity", "version", name="uq_intent_authority_profile_identity"),
        sa.CheckConstraint("version > 0", name="ck_intent_authority_profile_positive"),
        sa.CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_intent_authority_profile_environment"),
        sa.CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_intent_authority_profile_status"),
        sa.CheckConstraint("max_target_cardinality >= 0 and max_outbound_messages >= 0 and max_action_count >= 0 and max_retries >= 0", name="ck_intent_authority_profile_limits"),
    )
    op.create_table(
        "recipient_endpoints",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("transport", sa.String(80), nullable=False),
        sa.Column("canonical_address", sa.String(180), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "transport", "canonical_address", name="uq_recipient_endpoint_identity"),
        sa.CheckConstraint("status in ('ACTIVE','INACTIVE','RETIRED')", name="ck_recipient_endpoint_status"),
    )
    op.create_table(
        "scenario_version_authority_bindings",
        sa.Column("scenario_version_id", sa.String(64), sa.ForeignKey("scenario_versions.id"), primary_key=True),
        sa.Column("binding_role", sa.String(32), primary_key=True),
        sa.Column("execution_class_version_id", sa.String(64), sa.ForeignKey("execution_class_versions.id")),
        sa.Column("safety_set_version_id", sa.String(64), sa.ForeignKey("safety_set_versions.id")),
        sa.Column("policy_version_id", sa.String(64), sa.ForeignKey("policy_versions.id")),
        sa.Column("authority_profile_id", sa.String(64), sa.ForeignKey("static_intent_authority_profiles.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("binding_role in ('EXECUTION_CLASS','SAFETY_SET','POLICY','AUTHORITY_PROFILE')", name="ck_scenario_authority_binding_role"),
    )
    op.create_table(
        "execution_intent_target_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("execution_intent_id", sa.String(64), sa.ForeignKey("execution_intents.id"), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("recipient_endpoint_id", sa.String(64), sa.ForeignKey("recipient_endpoints.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("execution_intent_id", "target_type", name="uq_intent_target_type"),
        sa.UniqueConstraint("execution_intent_id", "ordinal", name="uq_intent_target_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_intent_target_ordinal"),
        sa.CheckConstraint("target_type = 'WHATSAPP_RECIPIENT_ENDPOINT'", name="ck_intent_target_type"),
    )
    op.add_column("scenario_versions", sa.Column("environment_classification", sa.String(24)))
    op.add_column("policy_versions", sa.Column("environment_classification", sa.String(24)))
    op.add_column("policy_versions", sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"))
    op.add_column("execution_intents", sa.Column("authority_profile_id", sa.String(64), sa.ForeignKey("static_intent_authority_profiles.id")))
    op.add_column("execution_intents", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("agent_execution_intents", sa.Column("execution_intent_id", sa.String(64), sa.ForeignKey("execution_intents.id")))
    op.add_column("agent_execution_intents", sa.Column("execution_intent_fingerprint", sa.String(128)))
    op.add_column("scenario_runs", sa.Column("agent_execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id")))
    op.create_index("ix_execution_intent_authority_profile", "execution_intents", ["authority_profile_id"])
    op.create_index("uq_agent_execution_intents_production_parent", "agent_execution_intents", ["execution_intent_id"], unique=True, postgresql_where=sa.text("execution_intent_id is not null"), sqlite_where=sa.text("execution_intent_id is not null"))
    op.create_index("ix_scenario_run_agent_execution_intent", "scenario_runs", ["agent_execution_intent_id"])
    op.create_index("uq_scenario_run_production_agent_intent", "scenario_runs", ["agent_execution_intent_id"], unique=True, postgresql_where=sa.text("agent_execution_intent_id is not null"), sqlite_where=sa.text("agent_execution_intent_id is not null"))
    op.create_check_constraint("ck_agent_execution_intent_bridge_pair", "agent_execution_intents", "(execution_intent_id is null and execution_intent_fingerprint is null) or (execution_intent_id is not null and execution_intent_fingerprint is not null)")
    op.create_index("ix_scenario_authority_binding_policy", "scenario_version_authority_bindings", ["policy_version_id"])
    op.create_check_constraint("ck_scenario_version_environment", "scenario_versions", "environment_classification is null or environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')")
    op.create_check_constraint("ck_policy_version_environment", "policy_versions", "environment_classification is null or environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')")
    op.create_check_constraint("ck_policy_version_status", "policy_versions", "status in ('ACTIVE','DEPRECATED','RETIRED')")
    op.execute("""
        CREATE FUNCTION reject_authority_semantic_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF (to_jsonb(OLD) - 'status' - 'enabled' - 'created_at') IS DISTINCT FROM
             (to_jsonb(NEW) - 'status' - 'enabled' - 'created_at') THEN
            RAISE EXCEPTION 'versioned authority semantics are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    for table in ("execution_class_versions", "safety_set_versions", "static_intent_authority_profiles", "policy_versions", "scenario_versions", "recipient_endpoints"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_authority_semantic_update()")
    op.execute("""
        CREATE FUNCTION reject_authority_binding_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'scenario authority bindings are append-only';
        END $$
    """)
    op.execute("CREATE TRIGGER scenario_authority_bindings_immutable BEFORE UPDATE OR DELETE ON scenario_version_authority_bindings FOR EACH ROW EXECUTE FUNCTION reject_authority_binding_update()")
    op.execute("""
        CREATE FUNCTION reject_frozen_execution_intent_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.state IN ('FROZEN', 'MATERIALIZED') AND (
             OLD.scope IS DISTINCT FROM NEW.scope
             OR OLD.scope_fingerprint IS DISTINCT FROM NEW.scope_fingerprint
             OR OLD.authority_profile_id IS DISTINCT FROM NEW.authority_profile_id
             OR OLD.expires_at IS DISTINCT FROM NEW.expires_at
             OR NEW.state NOT IN (OLD.state, 'MATERIALIZED')
          ) THEN
            RAISE EXCEPTION 'frozen execution authority is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER execution_intents_frozen_immutable BEFORE UPDATE ON execution_intents FOR EACH ROW EXECUTE FUNCTION reject_frozen_execution_intent_update()")
    op.execute("""
        CREATE FUNCTION reject_execution_intent_target_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE parent_state text;
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'execution intent target bindings are write-once';
          END IF;
          SELECT state INTO parent_state FROM execution_intents WHERE id = NEW.execution_intent_id;
          IF parent_state IN ('FROZEN', 'MATERIALIZED') THEN
            RAISE EXCEPTION 'frozen execution intent cannot receive a target';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER execution_intent_targets_immutable BEFORE INSERT OR UPDATE OR DELETE ON execution_intent_target_bindings FOR EACH ROW EXECUTE FUNCTION reject_execution_intent_target_mutation()")
    op.execute("""
        CREATE FUNCTION reject_production_bridge_rebind() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.execution_intent_id IS DISTINCT FROM NEW.execution_intent_id
             OR OLD.execution_intent_fingerprint IS DISTINCT FROM NEW.execution_intent_fingerprint
             OR (OLD.execution_intent_id IS NOT NULL AND (
                OLD.authorization_source IS DISTINCT FROM NEW.authorization_source
                OR OLD.intent_type IS DISTINCT FROM NEW.intent_type
                OR OLD.effective_response_snapshot IS DISTINCT FROM NEW.effective_response_snapshot
                OR OLD.recipient_reference IS DISTINCT FROM NEW.recipient_reference
                OR OLD.capability_name IS DISTINCT FROM NEW.capability_name
                OR OLD.capability_request IS DISTINCT FROM NEW.capability_request
             )) THEN
            RAISE EXCEPTION 'production agent intent bridge is write-once';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER agent_execution_intents_bridge_immutable BEFORE UPDATE ON agent_execution_intents FOR EACH ROW EXECUTE FUNCTION reject_production_bridge_rebind()")
    op.execute("""
        CREATE FUNCTION reject_production_run_rebind() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.agent_execution_intent_id IS DISTINCT FROM NEW.agent_execution_intent_id THEN
            RAISE EXCEPTION 'production scenario run bridge is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER scenario_runs_bridge_immutable BEFORE UPDATE ON scenario_runs FOR EACH ROW EXECUTE FUNCTION reject_production_run_rebind()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM scenario_runs WHERE agent_execution_intent_id IS NOT NULL")).scalar_one():
        raise RuntimeError("0026 downgrade requires no bridged scenario runs")
    if bind.execute(sa.text("SELECT count(*) FROM agent_execution_intents WHERE execution_intent_id IS NOT NULL")).scalar_one():
        raise RuntimeError("0026 downgrade requires no bridged agent intents")
    if bind.execute(sa.text("SELECT count(*) FROM execution_intents WHERE state = 'MATERIALIZED'")).scalar_one():
        raise RuntimeError("0026 downgrade requires no materialized execution intents")
    for table in ("scenario_version_authority_bindings", "execution_intent_target_bindings", "execution_class_versions", "safety_set_versions", "static_intent_authority_profiles", "recipient_endpoints"):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"0026 downgrade requires empty {table}")
    if bind.execute(sa.text("SELECT count(*) FROM execution_intents WHERE authority_profile_id IS NOT NULL OR expires_at IS NOT NULL")).scalar_one():
        raise RuntimeError("0026 downgrade requires no production intent extensions")
    op.drop_constraint("ck_policy_version_status", "policy_versions", type_="check")
    op.drop_constraint("ck_agent_execution_intent_bridge_pair", "agent_execution_intents", type_="check")
    op.drop_constraint("ck_policy_version_environment", "policy_versions", type_="check")
    op.drop_constraint("ck_scenario_version_environment", "scenario_versions", type_="check")
    op.execute("DROP TRIGGER IF EXISTS scenario_authority_bindings_immutable ON scenario_version_authority_bindings")
    op.execute("DROP FUNCTION IF EXISTS reject_authority_binding_update()")
    op.execute("DROP TRIGGER IF EXISTS execution_intent_targets_immutable ON execution_intent_target_bindings")
    op.execute("DROP FUNCTION IF EXISTS reject_execution_intent_target_mutation()")
    op.execute("DROP TRIGGER IF EXISTS execution_intents_frozen_immutable ON execution_intents")
    op.execute("DROP FUNCTION IF EXISTS reject_frozen_execution_intent_update()")
    op.execute("DROP TRIGGER IF EXISTS scenario_runs_bridge_immutable ON scenario_runs")
    op.execute("DROP FUNCTION IF EXISTS reject_production_run_rebind()")
    op.execute("DROP TRIGGER IF EXISTS agent_execution_intents_bridge_immutable ON agent_execution_intents")
    op.execute("DROP FUNCTION IF EXISTS reject_production_bridge_rebind()")
    for table in ("execution_class_versions", "safety_set_versions", "static_intent_authority_profiles", "policy_versions", "scenario_versions", "recipient_endpoints"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_authority_semantic_update()")
    op.drop_index("ix_scenario_authority_binding_policy", table_name="scenario_version_authority_bindings")
    op.drop_index("ix_execution_intent_authority_profile", table_name="execution_intents")
    op.drop_index("uq_scenario_run_production_agent_intent", table_name="scenario_runs")
    op.drop_index("ix_scenario_run_agent_execution_intent", table_name="scenario_runs")
    op.drop_index("uq_agent_execution_intents_production_parent", table_name="agent_execution_intents")
    op.drop_column("scenario_runs", "agent_execution_intent_id")
    op.drop_column("agent_execution_intents", "execution_intent_fingerprint")
    op.drop_column("agent_execution_intents", "execution_intent_id")
    op.drop_column("execution_intents", "expires_at")
    op.drop_column("execution_intents", "authority_profile_id")
    op.drop_column("policy_versions", "status")
    op.drop_column("policy_versions", "environment_classification")
    op.drop_column("scenario_versions", "environment_classification")
    for table in ("execution_intent_target_bindings", "scenario_version_authority_bindings", "recipient_endpoints", "static_intent_authority_profiles", "safety_set_versions", "execution_class_versions"):
        op.drop_table(table)
