"""V0.4A current client-location snapshot.

Revision ID: 0042_client_location_snapshot
Revises: 0041_client_session_authority
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_client_location_snapshot"
down_revision = "0041_client_session_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_location_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("human_identity_id", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float(), nullable=False),
        sa.Column("precision", sa.String(length=16), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_client_location_snapshots"),
        sa.ForeignKeyConstraint(
            ["human_identity_id"],
            ["human_identities.id"],
            name="fk_client_location_human_identity",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["client_devices.id"],
            name="fk_client_location_device",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_client_location_tenant",
        ),
        sa.UniqueConstraint(
            "device_id",
            "tenant_id",
            name="uq_client_location_device_tenant",
        ),
        sa.CheckConstraint(
            "latitude >= -90 and latitude <= 90",
            name="ck_client_location_latitude",
        ),
        sa.CheckConstraint(
            "longitude >= -180 and longitude <= 180",
            name="ck_client_location_longitude",
        ),
        sa.CheckConstraint(
            "accuracy_m > 0 and accuracy_m <= 10000",
            name="ck_client_location_accuracy",
        ),
        sa.CheckConstraint(
            "precision is null or precision in ('PRECISE','APPROXIMATE')",
            name="ck_client_location_precision",
        ),
    )
    op.create_index(
        "ix_client_location_human_tenant",
        "client_location_snapshots",
        ["human_identity_id", "tenant_id"],
    )


def downgrade():
    op.drop_index(
        "ix_client_location_human_tenant",
        table_name="client_location_snapshots",
    )
    op.drop_table("client_location_snapshots")
