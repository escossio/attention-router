"""Personal Context V2C cross-source entity resolution.

Revision ID: 0052_entity_resolution_v0
Revises: 0051_personal_context_bootstrap
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0052_entity_resolution_v0"
down_revision = "0051_personal_context_bootstrap"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "entity_resolution_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey(
                "tenants.id",
                name="fk_entity_resolution_candidate_tenant",
            ),
            nullable=False,
        ),
        sa.Column("left_actor_key", sa.String(120), nullable=False),
        sa.Column("right_actor_key", sa.String(120), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("decision_kind", sa.String(40), nullable=True),
        sa.Column("decision_actor_key", sa.String(120), nullable=True),
        sa.Column("decision_ref", sa.String(240), nullable=True),
        sa.Column("canonical_actor_key", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_entity_resolution_candidate_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "left_actor_key",
            "right_actor_key",
            "evidence_fingerprint",
            name="uq_entity_resolution_candidate_pair_evidence",
        ),
        sa.CheckConstraint(
            "left_actor_key < right_actor_key",
            name="ck_entity_resolution_candidate_pair_order",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_entity_resolution_candidate_confidence",
        ),
        sa.CheckConstraint(
            "evidence_count > 0",
            name="ck_entity_resolution_candidate_evidence_count",
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED','CONFIRMED','REJECTED','AMBIGUOUS','SUPERSEDED')",
            name="ck_entity_resolution_candidate_state",
        ),
    )
    op.create_index(
        "ix_entity_resolution_candidate_tenant_state",
        "entity_resolution_candidates",
        ["tenant_id", "state", "updated_at"],
    )

    op.create_table(
        "entity_resolution_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.String(64),
            sa.ForeignKey(
                "entity_resolution_candidates.id",
                name="fk_entity_resolution_evidence_candidate",
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("evidence_type", sa.String(48), nullable=False),
        sa.Column("source_ref", sa.String(240), nullable=False),
        sa.Column("independence_key", sa.String(160), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_entity_resolution_evidence_idempotency",
        ),
        sa.CheckConstraint(
            "evidence_type IN ("
            "'EXACT_PROVIDER_IDENTITY',"
            "'NORMALIZED_PHONE',"
            "'NORMALIZED_EMAIL',"
            "'OWNER_CONFIRMATION',"
            "'SHARED_STABLE_IDENTIFIER',"
            "'EXPLICIT_RELATIONSHIP',"
            "'SOURCE_ALIAS'"
            ")",
            name="ck_entity_resolution_evidence_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_entity_resolution_evidence_confidence",
        ),
    )
    op.create_index(
        "ix_entity_resolution_evidence_candidate",
        "entity_resolution_evidence",
        ["candidate_id", "created_at"],
    )

    op.create_table(
        "entity_alias_resolutions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey(
                "tenants.id",
                name="fk_entity_alias_resolution_tenant",
            ),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(64),
            sa.ForeignKey(
                "entity_resolution_candidates.id",
                name="fk_entity_alias_resolution_candidate",
            ),
            nullable=False,
        ),
        sa.Column("alias_actor_key", sa.String(120), nullable=False),
        sa.Column("canonical_actor_key", sa.String(120), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("decision_actor_key", sa.String(120), nullable=False),
        sa.Column("decision_ref", sa.String(240), nullable=False),
        sa.Column("revoked_by_actor_key", sa.String(120), nullable=True),
        sa.Column("revocation_ref", sa.String(240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "candidate_id",
            name="uq_entity_alias_resolution_candidate",
        ),
        sa.CheckConstraint(
            "alias_actor_key <> canonical_actor_key",
            name="ck_entity_alias_resolution_distinct_actor",
        ),
        sa.CheckConstraint(
            "state IN ('ACTIVE','REVOKED','SUPERSEDED')",
            name="ck_entity_alias_resolution_state",
        ),
    )
    op.create_index(
        "uq_entity_alias_resolution_active_alias",
        "entity_alias_resolutions",
        ["tenant_id", "alias_actor_key"],
        unique=True,
        postgresql_where=sa.text("state='ACTIVE'"),
        sqlite_where=sa.text("state='ACTIVE'"),
    )
    op.create_index(
        "ix_entity_alias_resolution_canonical",
        "entity_alias_resolutions",
        ["tenant_id", "canonical_actor_key", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "entity_alias_resolutions",
        "entity_resolution_evidence",
        "entity_resolution_candidates",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "ENTITY_RESOLUTION_DOWNGRADE_REQUIRES_DATA_EXPORT"
            )

    op.drop_index(
        "ix_entity_alias_resolution_canonical",
        table_name="entity_alias_resolutions",
    )
    op.drop_index(
        "uq_entity_alias_resolution_active_alias",
        table_name="entity_alias_resolutions",
    )
    op.drop_table("entity_alias_resolutions")

    op.drop_index(
        "ix_entity_resolution_evidence_candidate",
        table_name="entity_resolution_evidence",
    )
    op.drop_table("entity_resolution_evidence")

    op.drop_index(
        "ix_entity_resolution_candidate_tenant_state",
        table_name="entity_resolution_candidates",
    )
    op.drop_table("entity_resolution_candidates")
