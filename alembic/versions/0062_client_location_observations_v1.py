"""Preserve owner device-location history instead of overwriting the only sample.

Revision ID: 0062_location_context_v1
Revises: 0061_tts_outbox_constraint
"""

from alembic import op
import sqlalchemy as sa

revision = "0062_location_context_v1"
down_revision = "0061_tts_outbox_constraint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_location_observations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("human_identity_id", sa.String(64), sa.ForeignKey("human_identities.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("client_devices.id"), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float(), nullable=False),
        sa.Column("precision", sa.String(16)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "device_id", "captured_at",
            name="uq_client_location_observation_device_time",
        ),
        sa.CheckConstraint(
            "latitude >= -90 and latitude <= 90",
            name="ck_client_location_observation_lat",
        ),
        sa.CheckConstraint(
            "longitude >= -180 and longitude <= 180",
            name="ck_client_location_observation_lon",
        ),
        sa.CheckConstraint(
            "accuracy_m > 0 and accuracy_m <= 10000",
            name="ck_client_location_observation_accuracy",
        ),
        sa.CheckConstraint(
            "precision is null or precision in ('PRECISE','APPROXIMATE')",
            name="ck_client_location_observation_precision",
        ),
    )
    op.create_index(
        "ix_client_location_observation_owner_time",
        "client_location_observations",
        ["tenant_id", "human_identity_id", "captured_at"],
    )
    op.create_index(
        "ix_client_location_observation_device_time",
        "client_location_observations",
        ["tenant_id", "device_id", "captured_at"],
    )



def downgrade() -> None:
    bind = op.get_bind()
    # Do not silently discard captured personal history. An operator needs
    # an explicit privacy-aware export/purge decision before rollback.
    if bind.execute(sa.text(
        "SELECT 1 FROM client_location_observations LIMIT 1"
    )).first():
        raise RuntimeError("LOCATION_HISTORY_ROLLBACK_REQUIRES_EXPLICIT_EXPORT_OR_PURGE")
    op.drop_index(
        "ix_client_location_observation_device_time",
        table_name="client_location_observations",
    )
    op.drop_index(
        "ix_client_location_observation_owner_time",
        table_name="client_location_observations",
    )
    op.drop_table("client_location_observations")
