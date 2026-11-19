"""Persist authenticated offline location observations separately from current state.

Revision ID: 0062_location_history
Revises: 0061_tts_outbox_constraint
"""

from alembic import op
import sqlalchemy as sa

revision = "0062_location_history"
down_revision = "0061_tts_outbox_constraint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_location_observations",
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("client_devices.id"), primary_key=True),
        sa.Column("observation_id", sa.String(36), primary_key=True),
        sa.Column(
            "human_identity_id", sa.String(64), sa.ForeignKey("human_identities.id"), nullable=False
        ),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("accuracy_m", sa.Float, nullable=False),
        sa.Column("precision", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_history_latitude"),
        sa.CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_history_longitude"),
        sa.CheckConstraint("accuracy_m > 0 AND accuracy_m <= 10000", name="ck_history_accuracy"),
        sa.CheckConstraint("precision IN ('PRECISE','APPROXIMATE')", name="ck_history_precision"),
    )
    op.create_index(
        "ix_location_history_chronology",
        "client_location_observations",
        ["tenant_id", "human_identity_id", "device_id", "captured_at"],
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM client_location_observations LIMIT 1")).first():
        raise RuntimeError("LOCATION_HISTORY_NONEMPTY_DOWNGRADE_REQUIRES_RECONCILIATION")
    op.drop_table("client_location_observations")
