"""Personal Context V2D semantic episode builder.

Revision ID: 0053_semantic_episode_v0
Revises: 0052_entity_resolution_v0
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0053_semantic_episode_v0"
down_revision = "0052_entity_resolution_v0"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "semantic_episodes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", name="fk_semantic_episode_tenant"),
            nullable=False,
        ),
        sa.Column("episode_type", sa.String(80), nullable=False),
        sa.Column("semantic_key", sa.String(128), nullable=False),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_ref", sa.String(240), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity_class", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_episode_id",
            sa.String(64),
            sa.ForeignKey(
                "semantic_episodes.id",
                name="fk_semantic_episode_supersedes",
            ),
            nullable=True,
        ),
        sa.Column(
            "split_from_episode_id",
            sa.String(64),
            sa.ForeignKey(
                "semantic_episodes.id",
                name="fk_semantic_episode_split_from",
            ),
            nullable=True,
        ),
        sa.Column("merged_from_episode_ids", json_type, nullable=False),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "semantic_key",
            name="uq_semantic_episode_tenant_key",
        ),
        sa.CheckConstraint(
            "scope_type IN ('RESOURCE','RELATIONSHIP','THREAD','SEMANTIC_KEY')",
            name="ck_semantic_episode_scope_type",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','CLOSED','SUPERSEDED','SPLIT','MERGED')",
            name="ck_semantic_episode_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_semantic_episode_confidence",
        ),
        sa.CheckConstraint(
            "sensitivity_class IN ('NORMAL','PRIVATE','SECRET')",
            name="ck_semantic_episode_sensitivity",
        ),
        sa.CheckConstraint(
            "last_activity_at >= started_at",
            name="ck_semantic_episode_activity_order",
        ),
    )
    op.create_index(
        "ix_semantic_episode_scope_activity",
        "semantic_episodes",
        [
            "tenant_id",
            "episode_type",
            "scope_type",
            "scope_ref",
            "last_activity_at",
        ],
    )
    op.create_index(
        "ix_semantic_episode_tenant_state",
        "semantic_episodes",
        ["tenant_id", "state", "updated_at"],
    )

    op.create_table(
        "semantic_episode_memberships",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey(
                "tenants.id",
                name="fk_semantic_episode_membership_tenant",
            ),
            nullable=False,
        ),
        sa.Column(
            "episode_id",
            sa.String(64),
            sa.ForeignKey(
                "semantic_episodes.id",
                name="fk_semantic_episode_membership_episode",
            ),
            nullable=False,
        ),
        sa.Column("member_type", sa.String(32), nullable=False),
        sa.Column("member_ref", sa.String(240), nullable=False),
        sa.Column("association_reason", sa.String(48), nullable=False),
        sa.Column("association_source", sa.String(80), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("ambiguous", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "episode_id",
            "member_type",
            "member_ref",
            name="uq_semantic_episode_membership_member",
        ),
        sa.CheckConstraint(
            "member_type IN ('TIMELINE_EVENT','CONVERSATION_MESSAGE')",
            name="ck_semantic_episode_membership_type",
        ),
        sa.CheckConstraint(
            "association_reason IN ("
            "'EXACT_RESOURCE',"
            "'EXACT_RELATIONSHIP',"
            "'EXPLICIT_THREAD',"
            "'EXPLICIT_SEMANTIC_KEY'"
            ")",
            name="ck_semantic_episode_membership_reason",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_semantic_episode_membership_confidence",
        ),
    )
    op.create_index(
        "ix_semantic_episode_membership_lookup",
        "semantic_episode_memberships",
        ["tenant_id", "member_type", "member_ref"],
    )
    op.create_index(
        "ix_semantic_episode_membership_episode",
        "semantic_episode_memberships",
        ["episode_id", "observed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("semantic_episode_memberships", "semantic_episodes"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "SEMANTIC_EPISODE_DOWNGRADE_REQUIRES_DATA_EXPORT"
            )

    op.drop_index(
        "ix_semantic_episode_membership_episode",
        table_name="semantic_episode_memberships",
    )
    op.drop_index(
        "ix_semantic_episode_membership_lookup",
        table_name="semantic_episode_memberships",
    )
    op.drop_table("semantic_episode_memberships")

    op.drop_index(
        "ix_semantic_episode_tenant_state",
        table_name="semantic_episodes",
    )
    op.drop_index(
        "ix_semantic_episode_scope_activity",
        table_name="semantic_episodes",
    )
    op.drop_table("semantic_episodes")
