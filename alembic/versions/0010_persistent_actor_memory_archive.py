"""persistent actor memory and conversation archive

Revision ID: 0010_memory_archive
Revises: 0009_behavior_response_artifacts
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010_memory_archive"
down_revision: str | None = "0009_behavior_response_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
J = sa.JSON()


def upgrade() -> None:
    op.create_table("memory_actors", sa.Column("id", sa.String(64), primary_key=True), sa.Column("actor_key", sa.String(120), nullable=False, unique=True), sa.Column("metadata", J, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("conversation_threads", sa.Column("id", sa.String(64), primary_key=True), sa.Column("source", sa.String(120), nullable=False), sa.Column("source_account", sa.String(180), nullable=False), sa.Column("external_thread_key", sa.String(240), nullable=False), sa.Column("thread_type", sa.String(20), nullable=False), sa.Column("title", sa.String(240)), sa.Column("first_message_at", sa.DateTime(timezone=True)), sa.Column("last_message_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("source", "source_account", "external_thread_key", name="uq_memory_thread_external"))
    op.create_table("conversation_participants", sa.Column("id", sa.String(64), primary_key=True), sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversation_threads.id"), nullable=False), sa.Column("actor_id", sa.String(64), sa.ForeignKey("memory_actors.id")), sa.Column("external_participant_key", sa.String(240), nullable=False), sa.Column("observed_display_name", sa.String(240)), sa.Column("participant_role", sa.String(40), nullable=False), sa.Column("first_seen_at", sa.DateTime(timezone=True)), sa.Column("last_seen_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("conversation_id", "external_participant_key", name="uq_memory_participant"))
    op.create_table("conversation_messages", sa.Column("id", sa.String(64), primary_key=True), sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversation_threads.id"), nullable=False), sa.Column("source", sa.String(120), nullable=False), sa.Column("source_account", sa.String(180), nullable=False), sa.Column("source_message_id", sa.String(240), nullable=False), sa.Column("sender_actor_id", sa.String(64), sa.ForeignKey("memory_actors.id")), sa.Column("external_sender_key", sa.String(240)), sa.Column("direction", sa.String(20), nullable=False), sa.Column("from_me", sa.Boolean, nullable=False), sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False), sa.Column("message_type", sa.String(40), nullable=False), sa.Column("text", sa.Text), sa.Column("normalized_text", sa.Text), sa.Column("content_hash", sa.String(128), nullable=False), sa.Column("sensitivity_class", sa.String(20), nullable=False), sa.Column("searchable", sa.Boolean, nullable=False), sa.Column("metadata", J, nullable=False), sa.Column("reply_to_message_id", sa.String(64), sa.ForeignKey("conversation_messages.id")), sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("source", "source_account", "source_message_id", name="uq_memory_message_external"))
    op.create_index("ix_memory_message_conversation_sent", "conversation_messages", ["conversation_id", "sent_at"])
    op.create_index("ix_memory_message_hash", "conversation_messages", ["content_hash"])
    op.create_table("memory_extraction_runs", sa.Column("id", sa.String(64), primary_key=True), sa.Column("mode", sa.String(20), nullable=False), sa.Column("provider", sa.String(80), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("message_count", sa.Integer, nullable=False), sa.Column("error", sa.Text), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_table("memory_candidates", sa.Column("id", sa.String(64), primary_key=True), sa.Column("message_id", sa.String(64), sa.ForeignKey("conversation_messages.id"), nullable=False), sa.Column("extraction_run_id", sa.String(64), sa.ForeignKey("memory_extraction_runs.id"), nullable=False), sa.Column("subject", J, nullable=False), sa.Column("predicate", sa.String(160), nullable=False), sa.Column("object", J, nullable=False), sa.Column("confidence", sa.Float, nullable=False), sa.Column("sensitivity_class", sa.String(20), nullable=False), sa.Column("eligibility", sa.String(40), nullable=False), sa.Column("eligibility_reason", sa.Text, nullable=False), sa.Column("extraction_method", sa.String(80), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("memory_claims", sa.Column("id", sa.String(64), primary_key=True), sa.Column("subject_actor_id", sa.String(64), sa.ForeignKey("memory_actors.id")), sa.Column("subject_entity_id", sa.String(64)), sa.Column("predicate", sa.String(160), nullable=False), sa.Column("object_type", sa.String(40), nullable=False), sa.Column("object_text", sa.Text), sa.Column("object_actor_id", sa.String(64), sa.ForeignKey("memory_actors.id")), sa.Column("object_entity_id", sa.String(64)), sa.Column("object_json", J), sa.Column("context", J, nullable=False), sa.Column("confidence", sa.Float, nullable=False), sa.Column("sensitivity_class", sa.String(20), nullable=False), sa.Column("source_quality", sa.String(40), nullable=False), sa.Column("valid_from", sa.DateTime(timezone=True)), sa.Column("valid_until", sa.DateTime(timezone=True)), sa.Column("status", sa.String(30), nullable=False), sa.Column("staleness_class", sa.String(30), nullable=False), sa.Column("supersedes_claim_id", sa.String(64), sa.ForeignKey("memory_claims.id")), sa.Column("conflict_group_id", sa.String(64)), sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_memory_claim_subject_predicate", "memory_claims", ["subject_actor_id", "predicate"])
    op.create_index("ix_memory_claims_predicate", "memory_claims", ["predicate"])
    op.create_index("ix_memory_claims_status", "memory_claims", ["status"])
    op.create_table("memory_evidence", sa.Column("id", sa.String(64), primary_key=True), sa.Column("claim_id", sa.String(64), sa.ForeignKey("memory_claims.id"), nullable=False), sa.Column("conversation_message_id", sa.String(64), sa.ForeignKey("conversation_messages.id"), nullable=False), sa.Column("extraction_run_id", sa.String(64), sa.ForeignKey("memory_extraction_runs.id"), nullable=False), sa.Column("evidence_type", sa.String(60), nullable=False), sa.Column("confidence", sa.Float, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("memory_ingestion_jobs", sa.Column("id", sa.String(64), primary_key=True), sa.Column("source_message_id", sa.String(64), sa.ForeignKey("conversation_messages.id"), nullable=False, unique=True), sa.Column("mode", sa.String(20), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("attempt_count", sa.Integer, nullable=False), sa.Column("last_error", sa.Text), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_memory_jobs_status", "memory_ingestion_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_memory_jobs_status", table_name="memory_ingestion_jobs")
    op.drop_table("memory_ingestion_jobs")
    op.drop_table("memory_evidence")
    op.drop_index("ix_memory_claims_status", table_name="memory_claims")
    op.drop_index("ix_memory_claims_predicate", table_name="memory_claims")
    op.drop_index("ix_memory_claim_subject_predicate", table_name="memory_claims")
    op.drop_table("memory_claims")
    op.drop_table("memory_candidates")
    op.drop_table("memory_extraction_runs")
    op.drop_index("ix_memory_message_hash", table_name="conversation_messages")
    op.drop_index("ix_memory_message_conversation_sent", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_table("conversation_participants")
    op.drop_table("conversation_threads")
    op.drop_table("memory_actors")
