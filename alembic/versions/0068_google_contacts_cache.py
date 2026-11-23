"""Reproducible installation of Google Contacts tables found in production.

Revision ID: 0068_google_contacts_cache
Revises: 0067_vector_lock_order

Additive and check-first because these tables were originally installed by an
idempotent operational script. No existing contact records are modified.
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_google_contacts_cache"
down_revision = "0067_vector_lock_order"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
connections = sa.Table(
    "google_contacts_connections",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("owner_id", sa.String(64), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("envelope", sa.JSON(), nullable=False),
    sa.Column("generation", sa.String(64), nullable=False),
    sa.Column("next_run", sa.Float(), nullable=False),
    sa.Column("last_sync", sa.Float(), nullable=True),
    sa.Column("failures", sa.Integer(), nullable=False),
    sa.Column("error", sa.String(80), nullable=True),
)
cache = sa.Table(
    "google_contacts_cache",
    metadata,
    sa.Column(
        "connection_id", sa.String(64),
        sa.ForeignKey("google_contacts_connections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("resource", sa.String(240), primary_key=True),
    sa.Column("etag", sa.String(512)),
    sa.Column("person", sa.JSON(), nullable=False),
    sa.Column("search_name", sa.Text(), nullable=False),
    sa.Column("phones", sa.JSON(), nullable=False),
    sa.Column("emails", sa.JSON(), nullable=False),
    sa.Column("generation", sa.String(64), nullable=False),
    sa.Column("deleted", sa.Boolean(), nullable=False),
)
tenant_index = sa.Index(
    "ix_google_contacts_connections_tenant_id", connections.c.tenant_id
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (connections, cache):
        if inspector.has_table(table.name):
            # Do not silently accept a namesake table with incompatible columns.
            existing = {
                col["name"]: (str(col["type"]).lower(), col["nullable"])
                for col in sa.inspect(bind).get_columns(table.name)
            }
            for col in table.columns:
                if col.name not in existing:
                    raise RuntimeError("GOOGLE_CONTACTS_SCHEMA_COLUMN_MISSING")
                if bool(existing[col.name][1]) != bool(col.nullable):
                    raise RuntimeError("GOOGLE_CONTACTS_SCHEMA_NULLABILITY_MISMATCH")
        else:
            table.create(bind, checkfirst=True)
    tenant_index.create(bind, checkfirst=True)


def downgrade():
    """Preserve operational Contacts tables that existed before Alembic 0068.

    The production schema already included these objects when its revision
    marker was 0067. A downgrade that drops empty tables would destroy part
    of that pre-existing schema, while rejecting non-empty tables would
    make rollback impossible. Revert version metadata without removing the
    independently managed Contacts tables. On a brand-new database, this is
    deliberately a data-preserving, additive rollback rather than an exact
    removal of every table created by 0068.
    """
    bind = op.get_bind()
    for table in (connections, cache):
        if not sa.inspect(bind).has_table(table.name):
            raise RuntimeError("GOOGLE_CONTACTS_ROLLBACK_SCHEMA_MISSING")
    # No DROP TABLE and no DROP INDEX. Alembic updates its revision marker.
