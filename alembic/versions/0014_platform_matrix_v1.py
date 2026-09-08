"""platform matrix v1 core

Revision ID: 0014_platform_matrix_v1
Revises: 0013_lab_inbound_membership_pk
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0014_platform_matrix_v1"
down_revision = "0013_lab_inbound_membership_pk"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def json_type():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id",
        sa.String(64),
        nullable=False,
        server_default=DEFAULT_TENANT_ID,
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    stamp = datetime.now(timezone.utc)
    j = json_type()

    tenants = op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        tenants,
        [{
            "id": DEFAULT_TENANT_ID,
            "slug": "tenant_alex",
            "name": "Alex",
            "status": "ACTIVE",
            "created_at": stamp,
            "updated_at": stamp,
        }],
    )

    tenant_roots = (
        "policies",
        "interactions",
        "audit_events",
        "inbound_events",
        "actor_bindings",
        "agent_blueprints",
        "memory_actors",
        "conversation_threads",
        "conversation_messages",
        "memory_extraction_runs",
    )
    for table in tenant_roots:
        op.add_column(table, _tenant_column())
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        if dialect != "sqlite":
            op.create_foreign_key(
                f"fk_{table}_tenant",
                table,
                "tenants",
                ["tenant_id"],
                ["id"],
            )

    if dialect != "sqlite":
        op.drop_constraint("uq_inbound_source_external", "inbound_events", type_="unique")
        op.create_unique_constraint(
            "uq_inbound_tenant_source_external",
            "inbound_events",
            ["tenant_id", "source", "external_event_id"],
        )
        op.drop_constraint("uq_actor_bindings_source_external", "actor_bindings", type_="unique")
        op.create_unique_constraint(
            "uq_actor_bindings_tenant_source_external",
            "actor_bindings",
            ["tenant_id", "source", "external_actor_id"],
        )
        op.drop_constraint("memory_actors_actor_key_key", "memory_actors", type_="unique")
        op.create_unique_constraint(
            "uq_memory_actor_tenant_key", "memory_actors", ["tenant_id", "actor_key"]
        )
        op.drop_constraint("uq_memory_thread_external", "conversation_threads", type_="unique")
        op.create_unique_constraint(
            "uq_memory_thread_tenant_external",
            "conversation_threads",
            ["tenant_id", "source", "source_account", "external_thread_key"],
        )
        op.drop_constraint("uq_memory_message_external", "conversation_messages", type_="unique")
        op.create_unique_constraint(
            "uq_memory_message_tenant_external",
            "conversation_messages",
            ["tenant_id", "source", "source_account", "source_message_id"],
        )

    op.create_table(
        "resources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "resource_type", "canonical_name", name="uq_resource_tenant_type_name"),
    )
    op.create_index("ix_resources_tenant_type", "resources", ["tenant_id", "resource_type"])

    op.create_table(
        "relationships",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_entity_type", sa.String(40), nullable=False),
        sa.Column("source_entity_id", sa.String(120), nullable=False),
        sa.Column("target_entity_type", sa.String(40), nullable=False),
        sa.Column("target_entity_id", sa.String(120), nullable=False),
        sa.Column("relationship_type", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_relationship_source",
        "relationships",
        ["tenant_id", "source_entity_type", "source_entity_id"],
    )
    op.create_index(
        "ix_relationship_target",
        "relationships",
        ["tenant_id", "target_entity_type", "target_entity_id"],
    )

    op.create_table(
        "canonical_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("actor_id", sa.String(120)),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("channel", sa.String(80)),
        sa.Column("payload_type", sa.String(80), nullable=False),
        sa.Column("payload_ref", j, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("causation_id", sa.String(64)),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), unique=True),
        sa.Column("metadata_sanitized", j, nullable=False),
    )
    op.create_index("ix_canonical_events_correlation_id", "canonical_events", ["correlation_id"])
    op.create_index("ix_canonical_events_tenant_occurred", "canonical_events", ["tenant_id", "occurred_at"])

    op.create_table(
        "timeline_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("canonical_event_id", sa.String(64), sa.ForeignKey("canonical_events.id")),
        sa.Column("actor_id", sa.String(120)),
        sa.Column("relationship_id", sa.String(64), sa.ForeignKey("relationships.id")),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("event_ref", j, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visibility", sa.String(40), nullable=False),
        sa.Column("provenance", sa.String(80), nullable=False),
        sa.Column("metadata", j, nullable=False),
    )
    op.create_index(
        "ix_timeline_tenant_actor_occurred",
        "timeline_events",
        ["tenant_id", "actor_id", "occurred_at"],
    )
    op.create_index(
        "ix_timeline_tenant_resource_occurred",
        "timeline_events",
        ["tenant_id", "resource_id", "occurred_at"],
    )

    op.create_table(
        "facts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("predicate", sa.String(160), nullable=False),
        sa.Column("value", j),
        sa.Column("value_ref", sa.String(240)),
        sa.Column("fact_class", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("source_ref", sa.String(160)),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("supersedes_fact_id", sa.String(64), sa.ForeignKey("facts.id")),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_facts_tenant_subject_predicate",
        "facts",
        ["tenant_id", "subject_type", "subject_id", "predicate"],
    )

    op.create_table(
        "entity_states",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("subject_type", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("state_namespace", sa.String(80), nullable=False),
        sa.Column("state_key", sa.String(120), nullable=False),
        sa.Column("value", j, nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "subject_type", "subject_id", "state_namespace", "state_key",
            name="uq_entity_state_subject_key",
        ),
    )

    op.create_table(
        "capability_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("domain", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("availability_state", sa.String(40), nullable=False),
        sa.Column("current_version_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "canonical_name", name="uq_capability_tenant_name"),
    )
    op.create_index(
        "ix_capabilities_tenant_state",
        "capability_definitions",
        ["tenant_id", "availability_state"],
    )
    op.create_table(
        "capability_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("capability_id", sa.String(64), sa.ForeignKey("capability_definitions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(20), nullable=False),
        sa.Column("input_schema", j, nullable=False),
        sa.Column("output_schema", j, nullable=False),
        sa.Column("required_permissions", j, nullable=False),
        sa.Column("required_provider_interface", sa.String(120)),
        sa.Column("sensitivity", sa.String(40), nullable=False),
        sa.Column("side_effect", sa.Boolean(), nullable=False),
        sa.Column("default_approval_policy", sa.String(40), nullable=False),
        sa.Column("availability_state", sa.String(40), nullable=False),
        sa.Column("manifest_checksum", sa.String(128), nullable=False),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("capability_id", "version", name="uq_capability_version"),
        sa.UniqueConstraint("capability_id", "manifest_checksum", name="uq_capability_checksum"),
    )
    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_capability_current_version",
            "capability_definitions",
            "capability_versions",
            ["current_version_id"],
            ["id"],
        )

    op.create_table(
        "provider_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("canonical_name", sa.String(160), nullable=False, unique=True),
        sa.Column("interface_name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_provider_definitions_interface", "provider_definitions", ["interface_name"])
    op.create_table(
        "provider_instances",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider_definition_id", sa.String(64), sa.ForeignKey("provider_definitions.id"), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("health", sa.String(40), nullable=False),
        sa.Column("config_reference", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "canonical_name", name="uq_provider_instance_tenant_name"),
    )
    op.create_table(
        "provider_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("capability_id", sa.String(64), sa.ForeignKey("capability_definitions.id"), nullable=False),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("provider_instance_id", sa.String(64), sa.ForeignKey("provider_instances.id"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_provider_bindings_resolution",
        "provider_bindings",
        ["tenant_id", "capability_id", "resource_id", "status", "priority"],
    )
    op.create_table(
        "capability_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("grantor_type", sa.String(40), nullable=False),
        sa.Column("grantor_id", sa.String(120), nullable=False),
        sa.Column("grantee_type", sa.String(40), nullable=False),
        sa.Column("grantee_id", sa.String(120), nullable=False),
        sa.Column("capability_id", sa.String(64), sa.ForeignKey("capability_definitions.id"), nullable=False),
        sa.Column("scope", j, nullable=False),
        sa.Column("target_resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("constraints", j, nullable=False),
        sa.Column("provenance", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_capability_grants_resolution",
        "capability_grants",
        ["tenant_id", "grantee_type", "grantee_id", "capability_id", "status"],
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("canonical_name", sa.String(160), nullable=False),
        sa.Column("platform", sa.String(40), nullable=False),
        sa.Column("roles", j, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("metadata", j, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "canonical_name", name="uq_device_tenant_name"),
    )
    op.create_table(
        "device_identities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("identity_type", sa.String(80), nullable=False),
        sa.Column("identity_reference_hash", sa.String(128), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "identity_type", "identity_reference_hash",
            name="uq_device_identity_tenant_ref",
        ),
    )
    op.create_table(
        "device_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("actor_key", sa.String(120)),
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resources.id")),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actor_key is not null or resource_id is not null", name="ck_device_binding_target"),
    )
    op.create_index("ix_device_bindings_tenant_device", "device_bindings", ["tenant_id", "device_id"])
    op.create_table(
        "device_capabilities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("capability_name", sa.String(160), nullable=False),
        sa.Column("availability", sa.String(40), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", j, nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "device_id", "capability_name",
            name="uq_device_capability_announcement",
        ),
    )
    op.create_table(
        "device_statuses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("health", sa.String(40), nullable=False),
        sa.Column("connectivity", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics_sanitized", j, nullable=False),
    )
    op.create_index(
        "ix_device_status_device_observed",
        "device_statuses",
        ["tenant_id", "device_id", "observed_at"],
    )

    op.add_column(
        "agent_decisions",
        sa.Column("requested_capabilities", j, nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("agent_decisions", sa.Column("canonical_event_id", sa.String(64)))
    op.add_column("agent_decisions", sa.Column("semantic_source", sa.String(80)))
    op.add_column("agent_execution_intents", sa.Column("capability_name", sa.String(160)))
    op.add_column("agent_execution_intents", sa.Column("capability_request", j))
    op.add_column("agent_execution_intents", sa.Column("provider_instance_id", sa.String(64)))
    op.add_column("agent_execution_intents", sa.Column("canonical_event_id", sa.String(64)))
    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_agent_decisions_canonical_event",
            "agent_decisions",
            "canonical_events",
            ["canonical_event_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_execution_provider_instance",
            "agent_execution_intents",
            "provider_instances",
            ["provider_instance_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_execution_canonical_event",
            "agent_execution_intents",
            "canonical_events",
            ["canonical_event_id"],
            ["id"],
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        op.drop_constraint("fk_execution_canonical_event", "agent_execution_intents", type_="foreignkey")
        op.drop_constraint("fk_execution_provider_instance", "agent_execution_intents", type_="foreignkey")
        op.drop_constraint("fk_agent_decisions_canonical_event", "agent_decisions", type_="foreignkey")
    for column in ("canonical_event_id", "provider_instance_id", "capability_request", "capability_name"):
        op.drop_column("agent_execution_intents", column)
    for column in ("semantic_source", "canonical_event_id", "requested_capabilities"):
        op.drop_column("agent_decisions", column)

    op.drop_index("ix_device_status_device_observed", table_name="device_statuses")
    op.drop_table("device_statuses")
    op.drop_table("device_capabilities")
    op.drop_index("ix_device_bindings_tenant_device", table_name="device_bindings")
    op.drop_table("device_bindings")
    op.drop_table("device_identities")
    op.drop_table("devices")
    op.drop_index("ix_capability_grants_resolution", table_name="capability_grants")
    op.drop_table("capability_grants")
    op.drop_index("ix_provider_bindings_resolution", table_name="provider_bindings")
    op.drop_table("provider_bindings")
    op.drop_table("provider_instances")
    op.drop_index("ix_provider_definitions_interface", table_name="provider_definitions")
    op.drop_table("provider_definitions")
    if dialect != "sqlite":
        op.drop_constraint(
            "fk_capability_current_version",
            "capability_definitions",
            type_="foreignkey",
        )
    op.drop_table("capability_versions")
    op.drop_index("ix_capabilities_tenant_state", table_name="capability_definitions")
    op.drop_table("capability_definitions")
    op.drop_table("entity_states")
    op.drop_index("ix_facts_tenant_subject_predicate", table_name="facts")
    op.drop_table("facts")
    op.drop_index("ix_timeline_tenant_resource_occurred", table_name="timeline_events")
    op.drop_index("ix_timeline_tenant_actor_occurred", table_name="timeline_events")
    op.drop_table("timeline_events")
    op.drop_index("ix_canonical_events_tenant_occurred", table_name="canonical_events")
    op.drop_index("ix_canonical_events_correlation_id", table_name="canonical_events")
    op.drop_table("canonical_events")
    op.drop_index("ix_relationship_target", table_name="relationships")
    op.drop_index("ix_relationship_source", table_name="relationships")
    op.drop_table("relationships")
    op.drop_index("ix_resources_tenant_type", table_name="resources")
    op.drop_table("resources")

    if dialect != "sqlite":
        op.drop_constraint(
            "uq_actor_bindings_tenant_source_external", "actor_bindings", type_="unique"
        )
        op.create_unique_constraint(
            "uq_actor_bindings_source_external",
            "actor_bindings",
            ["source", "external_actor_id"],
        )
        op.drop_constraint(
            "uq_inbound_tenant_source_external", "inbound_events", type_="unique"
        )
        op.create_unique_constraint(
            "uq_inbound_source_external",
            "inbound_events",
            ["source", "external_event_id"],
        )
        op.drop_constraint("uq_memory_thread_tenant_external", "conversation_threads", type_="unique")
        op.create_unique_constraint(
            "uq_memory_thread_external",
            "conversation_threads",
            ["source", "source_account", "external_thread_key"],
        )
        op.drop_constraint(
            "uq_memory_message_tenant_external", "conversation_messages", type_="unique"
        )
        op.create_unique_constraint(
            "uq_memory_message_external",
            "conversation_messages",
            ["source", "source_account", "source_message_id"],
        )
        op.drop_constraint("uq_memory_actor_tenant_key", "memory_actors", type_="unique")
        op.create_unique_constraint("memory_actors_actor_key_key", "memory_actors", ["actor_key"])

    tenant_roots = (
        "memory_extraction_runs",
        "conversation_messages",
        "conversation_threads",
        "memory_actors",
        "agent_blueprints",
        "actor_bindings",
        "inbound_events",
        "audit_events",
        "interactions",
        "policies",
    )
    for table in tenant_roots:
        if dialect != "sqlite":
            op.drop_constraint(f"fk_{table}_tenant", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_column(table, "tenant_id")
    op.drop_table("tenants")
