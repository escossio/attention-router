"""Persist explicit source consent for the staged reader."""

from alembic import op
import sqlalchemy as sa

revision = "0071_context_source_consent"
down_revision = "0070_context_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "context_consent_scopes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("source_account", sa.String(180), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("scope_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("incarnation", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("revision >= 0", name="ck_context_consent_revision"),
    )
    op.create_table(
        "context_source_consents",
        sa.Column("scope_id", sa.String(64), sa.ForeignKey("context_consent_scopes.id"), primary_key=True),
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("decision_ref", sa.String(120)),
        sa.Column("conversation_id", sa.String(64)),
        sa.Column("receipt_id", sa.String(64)),
        sa.Column("message_revision", sa.String(36)),
        sa.Column("receipt_revision", sa.String(36)),
        sa.Column("content_digest", sa.String(64)),
        sa.Column("sensitivity", sa.String(20)),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("retention", sa.JSON(none_as_null=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "revoked_at IS NOT NULL OR (decision_ref IS NOT NULL AND conversation_id IS NOT NULL "
            "AND receipt_id IS NOT NULL AND message_revision IS NOT NULL AND receipt_revision IS NOT NULL "
            "AND content_digest IS NOT NULL AND sensitivity IS NOT NULL AND starts_at IS NOT NULL "
            "AND expires_at IS NOT NULL AND starts_at < expires_at AND retention IS NOT NULL)",
            name="ck_context_source_consent_complete",
        ),
    )

    op.execute("ALTER TABLE context_consent_scopes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE context_source_consents ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.execute("LOCK TABLE context_consent_scopes, context_source_consents IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM context_consent_scopes) THEN
                RAISE EXCEPTION 'CONTEXT_CONSENT_DOWNGRADE_REQUIRES_EMPTY_AUTHORITY';
            END IF;
        END $$;
    """)
    op.drop_table("context_source_consents")
    op.drop_table("context_consent_scopes")
