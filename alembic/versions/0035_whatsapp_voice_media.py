"""Persistent WhatsApp voice media, transcription, and TTS derivation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0035_whatsapp_voice_media"
down_revision = "0034_global_direct_grace"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "media_artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id")),
        sa.Column("execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id")),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("media_kind", sa.String(32), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("provenance", json_type, nullable=False),
        sa.CheckConstraint("direction in ('INBOUND','OUTBOUND')", name="ck_media_artifact_direction"),
        sa.CheckConstraint("purpose in ('VOICE_INPUT','TTS_OUTPUT')", name="ck_media_artifact_purpose"),
        sa.CheckConstraint("status in ('PENDING','READY','FAILED','DELETED','AMBIGUOUS')", name="ck_media_artifact_status"),
    )
    op.create_index("uq_media_artifact_inbound_voice", "media_artifacts", ["inbound_event_id"], unique=True, postgresql_where=sa.text("purpose='VOICE_INPUT'"))
    op.create_index("uq_media_artifact_intent_tts", "media_artifacts", ["execution_intent_id"], unique=True, postgresql_where=sa.text("purpose='TTS_OUTPUT'"))
    op.create_table(
        "voice_transcriptions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("inbound_event_id", sa.String(64), sa.ForeignKey("inbound_events.id"), nullable=False, unique=True),
        sa.Column("media_artifact_id", sa.String(64), sa.ForeignKey("media_artifacts.id")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transcript_text", sa.Text()),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(80)),
        sa.Column("provider_request_reference", sa.String(180)),
        sa.Column("error_code", sa.String(120)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status in ('PENDING','PROCESSING','READY','FAILED')", name="ck_voice_transcription_status"),
        sa.CheckConstraint("(status='READY' and transcript_text is not null) or (status<>'READY' and transcript_text is null)", name="ck_voice_transcription_ready_text"),
    )
    op.create_table(
        "tts_derivations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("execution_intent_id", sa.String(64), sa.ForeignKey("agent_execution_intents.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_text_hash", sa.String(64), nullable=False),
        sa.Column("media_artifact_id", sa.String(64), sa.ForeignKey("media_artifacts.id")),
        sa.Column("provider", sa.String(32)),
        sa.Column("request_reference", sa.String(180)),
        sa.Column("error_code", sa.String(120)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status in ('PENDING','PROCESSING','READY','FAILED')", name="ck_tts_derivation_status"),
    )


def downgrade():
    bind = op.get_bind()
    for table in ("tts_derivations", "voice_transcriptions", "media_artifacts"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("WHATSAPP_VOICE_MEDIA_DOWNGRADE_REQUIRES_DATA_EXPORT")
    op.drop_table("tts_derivations")
    op.drop_table("voice_transcriptions")
    op.drop_index("uq_media_artifact_intent_tts", table_name="media_artifacts")
    op.drop_index("uq_media_artifact_inbound_voice", table_name="media_artifacts")
    op.drop_table("media_artifacts")
