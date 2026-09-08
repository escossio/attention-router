from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from attention_router.infrastructure.db import Base
from attention_router.core.tenancy import DEFAULT_TENANT_ID


JsonType = JSON().with_variant(JSONB, "postgresql")


class PolicyRow(Base):
    __tablename__ = "policies"
    identifier: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    match_criteria: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    specificity: Mapped[int] = mapped_column(Integer, nullable=False)
    tone: Mapped[str] = mapped_column(String(80), nullable=False)
    initial_wait_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_disclosures: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    allowed_actions: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    escalation_steps: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    ack_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    repetition_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    cancellation_conditions: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    completion_conditions: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PolicyVersionRow(Base):
    __tablename__ = "policy_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(80), ForeignKey("policies.identifier"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    environment_classification: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        UniqueConstraint("policy_id", "checksum", name="uq_policy_versions_policy_checksum"),
        CheckConstraint("environment_classification is null or environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_policy_version_environment"),
        CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_policy_version_status"),
    )


class InteractionRow(Base):
    __tablename__ = "interactions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    contact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(160), nullable=False)
    relationship_category: Mapped[str] = mapped_column(String(120), nullable=False)
    active_context: Mapped[str | None] = mapped_column(String(120))
    inbound_text: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("policies.identifier"))
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    lia_speech: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(80), ForeignKey("policies.identifier"), nullable=False)
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    matched_rules: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActionAttemptRow(Base):
    __tablename__ = "action_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    action_key: Mapped[str] = mapped_column(String(80), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False)
    outbox_message_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("outbox_messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TimerRow(Base):
    __tablename__ = "timers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    action_attempt_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("action_attempts.id"))
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AcknowledgementRow(Base):
    __tablename__ = "acknowledgements"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    action_attempt_id: Mapped[str] = mapped_column(String(64), ForeignKey("action_attempts.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    interaction_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("interactions.id"))
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    previous_state: Mapped[str | None] = mapped_column(String(80))
    next_state: Mapped[str | None] = mapped_column(String(80))
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    origin: Mapped[str] = mapped_column(String(120), nullable=False, default="core")
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QueueRow(Base):
    __tablename__ = "queue"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("id", name="uq_queue_id"),)


class InboundEventRow(Base):
    __tablename__ = "inbound_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(180), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interaction_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("interactions.id"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lineage_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="HISTORICAL_UNKNOWN",
        server_default="HISTORICAL_UNKNOWN",
    )
    scenario_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_runs.id",
            name="fk_inbound_events_scenario_run",
            use_alter=True,
        ),
    )
    scenario_step_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_step_runs.id",
            name="fk_inbound_events_scenario_step_run",
            use_alter=True,
        ),
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "external_event_id", name="uq_inbound_tenant_source_external"
        ),
        CheckConstraint(
            "lineage_classification in "
            "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
            name="ck_inbound_event_lineage_classification",
        ),
        Index("ix_inbound_events_scenario_run", "tenant_id", "scenario_run_id"),
    )


class MediaArtifactRow(Base):
    __tablename__ = "media_artifacts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    inbound_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("inbound_events.id"))
    execution_intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_execution_intents.id"))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    __table_args__ = (
        CheckConstraint("direction in ('INBOUND','OUTBOUND')", name="ck_media_artifact_direction"),
        CheckConstraint("purpose in ('VOICE_INPUT','TTS_OUTPUT')", name="ck_media_artifact_purpose"),
        CheckConstraint("status in ('PENDING','READY','FAILED','DELETED','AMBIGUOUS')", name="ck_media_artifact_status"),
        Index("uq_media_artifact_inbound_voice", "inbound_event_id", unique=True, postgresql_where=text("purpose='VOICE_INPUT'"), sqlite_where=text("purpose='VOICE_INPUT'")),
        Index("uq_media_artifact_intent_tts", "execution_intent_id", unique=True, postgresql_where=text("purpose='TTS_OUTPUT'"), sqlite_where=text("purpose='TTS_OUTPUT'")),
    )


class VoiceTranscriptionRow(Base):
    __tablename__ = "voice_transcriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    inbound_event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False, unique=True)
    media_artifact_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("media_artifacts.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    transcript_text: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(80))
    provider_request_reference: Mapped[str | None] = mapped_column(String(180))
    error_code: Mapped[str | None] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status in ('PENDING','PROCESSING','READY','FAILED')", name="ck_voice_transcription_status"),
        CheckConstraint("(status='READY' and transcript_text is not null) or (status<>'READY' and transcript_text is null)", name="ck_voice_transcription_ready_text"),
    )


class TTSDerivationRow(Base):
    __tablename__ = "tts_derivations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_intent_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_execution_intents.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_artifact_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("media_artifacts.id"))
    provider: Mapped[str | None] = mapped_column(String(32))
    request_reference: Mapped[str | None] = mapped_column(String(180))
    error_code: Mapped[str | None] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status in ('PENDING','PROCESSING','READY','FAILED')", name="ck_tts_derivation_status"),
    )


class ConversationResponseGraceWindowRow(Base):
    __tablename__ = "conversation_response_grace_windows"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False, default="default")
    channel: Mapped[str] = mapped_column(String(80), nullable=False)
    conversation_key: Mapped[str] = mapped_column(String(240), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_binding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("actor_bindings.id"))
    audience: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(String(64), ForeignKey("policy_versions.id"), nullable=False)
    represented_owner_actor_key: Mapped[str | None] = mapped_column(String(120))
    operational_control_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("owner_operational_controls.id")
    )
    operational_control_revision: Mapped[int | None] = mapped_column(Integer)
    operational_control_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="POLICY_DEFAULT", server_default="POLICY_DEFAULT"
    )
    auto_release_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    effective_grace_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    anchor_event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False)
    anchor_interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_by_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("inbound_events.id"))
    cancellation_reason: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_generation: Mapped[int | None] = mapped_column(Integer)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("state in ('OPEN','CANCELED','RELEASED','SUPERSEDED')", name="ck_response_grace_state"),
        CheckConstraint("generation > 0", name="ck_response_grace_generation_positive"),
        CheckConstraint("due_at >= last_inbound_at", name="ck_response_grace_due_after_inbound"),
        CheckConstraint(
            "operational_control_revision is null or operational_control_revision > 0",
            name="ck_response_grace_control_revision_positive",
        ),
        CheckConstraint(
            "operational_control_source in ('POLICY_DEFAULT','GLOBAL_DEFAULT','OWNER_OVERRIDE')",
            name="ck_response_grace_control_source",
        ),
        CheckConstraint(
            "effective_grace_seconds >= 0",
            name="ck_response_grace_effective_seconds_nonnegative",
        ),
        Index(
            "uq_response_grace_open_conversation",
            "tenant_id", "source", "source_account", "conversation_key",
            unique=True,
            postgresql_where=text("state = 'OPEN'"),
            sqlite_where=text("state = 'OPEN'"),
        ),
        Index("ix_response_grace_due", "due_at", "id", postgresql_where=text("state = 'OPEN'"), sqlite_where=text("state = 'OPEN'")),
        Index(
            "ix_response_grace_open_owner",
            "tenant_id", "represented_owner_actor_key", "policy_version_id",
            postgresql_where=text("state = 'OPEN'"),
            sqlite_where=text("state = 'OPEN'"),
        ),
    )


class ConversationResponseGraceInboundRow(Base):
    __tablename__ = "conversation_response_grace_inbounds"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grace_window_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversation_response_grace_windows.id"), nullable=False
    )
    inbound_event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    associated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("inbound_event_id", name="uq_response_grace_inbound_event"),
        UniqueConstraint("grace_window_id", "interaction_id", name="uq_response_grace_window_interaction"),
        CheckConstraint("generation > 0", name="ck_response_grace_inbound_generation_positive"),
        Index("ix_response_grace_inbounds_window_generation", "grace_window_id", "generation"),
    )


class OwnerAutomationControlRow(Base):
    __tablename__ = "owner_automation_controls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    represented_owner_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    automatic_responses_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    authorization_source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(180), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "represented_owner_actor_key", name="uq_owner_automation_scope"),
        CheckConstraint("revision >= 0", name="ck_owner_automation_revision"),
    )


class OwnerAutomationControlChangeRow(Base):
    __tablename__ = "owner_automation_control_changes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    control_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owner_automation_controls.id"), nullable=False
    )
    represented_owner_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(180), nullable=False)
    requested_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    previous_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_channel", "source_event_id", name="uq_owner_automation_change_event"
        ),
        CheckConstraint(
            "previous_revision >= 0 and resulting_revision >= previous_revision",
            name="ck_owner_automation_change_revisions",
        ),
        Index("ix_owner_automation_changes_control", "control_id", "created_at"),
    )


class OwnerOperationalControlRow(Base):
    __tablename__ = "owner_operational_controls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    represented_owner_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    control_key: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("policies.identifier"))
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="POLICY", server_default="POLICY")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    integer_value: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    authorization_source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(180))
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("(scope_type = 'GLOBAL' and policy_id is null) or "
                        "(scope_type = 'POLICY' and policy_id is not null)",
                        name="ck_owner_operational_scope"),
        Index("uq_owner_operational_global", "tenant_id", "represented_owner_actor_key", "control_key",
              unique=True, postgresql_where=text("scope_type = 'GLOBAL'"),
              sqlite_where=text("scope_type = 'GLOBAL'")),
        UniqueConstraint(
            "tenant_id", "represented_owner_actor_key", "control_key", "policy_id",
            name="uq_owner_operational_control_scope",
        ),
        CheckConstraint("revision > 0", name="ck_owner_operational_control_revision_positive"),
        CheckConstraint("integer_value >= 0", name="ck_owner_operational_control_integer_nonnegative"),
        Index(
            "ix_owner_operational_control_lookup",
            "tenant_id", "represented_owner_actor_key", "control_key", "policy_id",
        ),
    )


class OwnerOperationalControlChangeRow(Base):
    __tablename__ = "owner_operational_control_changes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    control_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("owner_operational_controls.id"), nullable=False
    )
    represented_owner_actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    control_key: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("policies.identifier"))
    source_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(180))
    requested_enabled: Mapped[bool | None] = mapped_column(Boolean)
    requested_integer_value: Mapped[int | None] = mapped_column(Integer)
    previous_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_channel", "source_event_id",
            name="uq_owner_operational_control_change_event",
        ),
        CheckConstraint(
            "previous_revision >= 0 and resulting_revision > 0",
            name="ck_owner_operational_control_change_revisions",
        ),
        CheckConstraint(
            "requested_integer_value is null or requested_integer_value >= 0",
            name="ck_owner_operational_control_change_integer_nonnegative",
        ),
        Index("ix_owner_operational_control_changes_control", "control_id", "created_at"),
    )


class ActorBindingRow(Base):
    __tablename__ = "actor_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    external_actor_id: Mapped[str] = mapped_column(String(180), nullable=False)
    actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    actor_category: Mapped[str] = mapped_column(String(120), nullable=False)
    active_context: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    binding_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "external_actor_id",
            name="uq_actor_bindings_tenant_source_external",
        ),
    )


class OutboxMessageRow(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    destination: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    execution_intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_execution_intents.id"))
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_outbox_idempotency_key"),)


class MetaDeliveryReconciliationRow(Base):
    __tablename__ = "meta_delivery_reconciliations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False
    )
    outbox_message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("outbox_messages.id"), nullable=False
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    api_accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closure_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_reconcile_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    terminal_reason: Mapped[str | None] = mapped_column(String(120))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operational_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(120))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantine_reason: Mapped[str | None] = mapped_column(String(160))
    last_requeue_audit_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("audit_events.id")
    )
    __table_args__ = (
        UniqueConstraint(
            "outbox_message_id", name="uq_meta_delivery_reconciliation_outbox"
        ),
        UniqueConstraint(
            "provider_message_id", name="uq_meta_delivery_reconciliation_provider"
        ),
        CheckConstraint(
            "state in ('PENDING','PASSED','FAILED')",
            name="ck_meta_delivery_reconciliation_state",
        ),
        CheckConstraint(
            "evidence_count >= 0",
            name="ck_meta_delivery_reconciliation_evidence_count",
        ),
        CheckConstraint(
            "deadline_at > api_accepted_at and closure_after > deadline_at",
            name="ck_meta_delivery_reconciliation_windows",
        ),
        CheckConstraint(
            "(state = 'PENDING' and terminal_reason is null and terminal_at is null) "
            "or (state in ('PASSED','FAILED') and terminal_reason is not null "
            "and terminal_at is not null)",
            name="ck_meta_delivery_reconciliation_terminal",
        ),
        CheckConstraint(
            "operational_state in ('ACTIVE','QUARANTINED')",
            name="ck_meta_delivery_reconciliation_operational_state",
        ),
        CheckConstraint(
            "failure_count >= 0",
            name="ck_meta_delivery_reconciliation_failure_count",
        ),
        CheckConstraint(
            "(operational_state = 'ACTIVE' and quarantined_at is null and quarantine_reason is null) "
            "or (operational_state = 'QUARANTINED' and state = 'PENDING' "
            "and quarantined_at is not null and quarantine_reason is not null)",
            name="ck_meta_delivery_reconciliation_quarantine",
        ),
        Index(
            "ix_meta_delivery_reconciliation_due",
            "next_reconcile_at",
            "id",
            postgresql_where=text("state = 'PENDING' AND operational_state = 'ACTIVE'"),
            sqlite_where=text("state = 'PENDING' AND operational_state = 'ACTIVE'"),
        ),
        Index(
            "ix_meta_delivery_reconciliation_quarantined",
            "quarantined_at",
            "id",
            postgresql_where=text("operational_state = 'QUARANTINED'"),
            sqlite_where=text("operational_state = 'QUARANTINED'"),
        ),
    )


class MetaCallbackInboxRow(Base):
    __tablename__ = "meta_callback_inbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    provider_timestamp_raw: Mapped[str | None] = mapped_column(String(64))
    provider_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    errors_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_fingerprint: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    reconciliation_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("meta_delivery_reconciliations.id")
    )
    correlation_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_correlation_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    correlated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_requeue_audit_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("audit_events.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "provider_message_id",
            "deduplication_key",
            name="uq_meta_callback_inbox_provider_deduplication",
        ),
        UniqueConstraint(
            "claim_token", name="uq_meta_callback_inbox_claim_token"
        ),
        CheckConstraint(
            "state in ('PENDING','CORRELATED','QUARANTINED')",
            name="ck_meta_callback_inbox_state",
        ),
        CheckConstraint(
            "correlation_attempt_count >= 0",
            name="ck_meta_callback_inbox_attempt_count",
        ),
        CheckConstraint(
            "((state = 'PENDING' and reconciliation_id is null and correlated_at is null "
            "and quarantined_at is null) or "
            "(state = 'CORRELATED' and reconciliation_id is not null and correlated_at is not null "
            "and quarantined_at is null) or "
            "(state = 'QUARANTINED' and reconciliation_id is null and correlated_at is null "
            "and quarantined_at is not null)) and "
            "((claim_token is null and claimed_by is null and claimed_at is null) or "
            "(state = 'PENDING' and claim_token is not null and claimed_by is not null "
            "and claimed_at is not null))",
            name="ck_meta_callback_inbox_lifecycle",
        ),
        Index(
            "ix_meta_callback_inbox_due",
            "next_correlation_at",
            "id",
            postgresql_where=text("state = 'PENDING'"),
            sqlite_where=text("state = 'PENDING'"),
        ),
        Index(
            "ix_meta_callback_inbox_provider_state",
            "provider_message_id",
            "state",
            "id",
        ),
        Index(
            "ix_meta_callback_inbox_provider_recoverable",
            "provider_message_id",
            "received_at",
            "id",
            postgresql_where=text(
                "state = 'PENDING' OR (state = 'QUARANTINED' AND "
                "last_error_code = 'PRODUCTION_META_RECONCILIATION_NOT_FOUND')"
            ),
        ),
    )


class MetaCallbackEvidenceRow(Base):
    __tablename__ = "meta_callback_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meta_delivery_reconciliations.id"), nullable=False
    )
    inbox_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("meta_callback_inbox.id"), unique=True
    )
    deduplication_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    provider_timestamp_raw: Mapped[str | None] = mapped_column(String(64))
    provider_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    admissible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    errors_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "reconciliation_id",
            "deduplication_key",
            name="uq_meta_callback_evidence_deduplication",
        ),
        Index(
            "ix_meta_callback_evidence_reconciliation_received",
            "reconciliation_id",
            "received_at",
            "id",
        ),
    )


class MetaCallbackAuditMarkerRow(Base):
    __tablename__ = "meta_callback_audit_markers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("meta_delivery_reconciliations.id"), nullable=False
    )
    marker_type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "reconciliation_id", "marker_type", name="uq_meta_callback_audit_marker"
        ),
    )


class AgentBlueprintRow(Base):
    __tablename__ = "agent_blueprints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentBlueprintVersionRow(Base):
    __tablename__ = "agent_blueprint_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    blueprint_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_blueprints.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    change_reason: Mapped[str] = mapped_column(String(240), nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("blueprint_id", "version", name="uq_agent_blueprint_versions_blueprint_version"),
    )


class ConfigurationSessionRow(Base):
    __tablename__ = "configuration_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    blueprint_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_blueprints.id"))
    current_stage: Mapped[str] = mapped_column(String(80), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentDecisionRow(Base):
    __tablename__ = "agent_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    agent_blueprint_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_blueprints.id"))
    agent_blueprint_version: Mapped[int | None] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(String(120))
    actor_binding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("actor_bindings.id"))
    audience: Mapped[str | None] = mapped_column(String(160))
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    decision_pipeline_version: Mapped[str] = mapped_column(String(40), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(60), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(120), nullable=False)
    proposed_response: Mapped[str | None] = mapped_column(Text)
    response_message_family: Mapped[str | None] = mapped_column(String(80))
    response_variant_id: Mapped[str | None] = mapped_column(String(120))
    response_spoken_text: Mapped[str | None] = mapped_column(Text)
    response_introduction_included: Mapped[bool | None] = mapped_column(Boolean)
    escalation_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(240))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    missing_information: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(80))
    objective: Mapped[str | None] = mapped_column(String(120))
    self_contained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context_sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context_requirements: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    requested_capabilities: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False, default=list)
    canonical_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("canonical_events.id"))
    semantic_source: Mapped[str | None] = mapped_column(String(80))
    response_source: Mapped[str | None] = mapped_column(String(80))
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_delivery_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasoning_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("event_id", "decision_pipeline_version", name="uq_agent_decision_event_pipeline"),
    )


class AutonomyEvaluationRow(Base):
    __tablename__ = "autonomy_evaluations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_decisions.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), nullable=False)
    interaction_id: Mapped[str] = mapped_column(String(64), ForeignKey("interactions.id"), nullable=False)
    agent_blueprint_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_blueprints.id"))
    agent_blueprint_version: Mapped[int | None] = mapped_column(Integer)
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    blueprint_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(60), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(120), nullable=False)
    action_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor_scope_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    audience_scope_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    freshness_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    from_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automatic_execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    conversation_contract_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("agent_decision_id", name="uq_autonomy_evaluations_decision"),
    )


class AgentResponseReviewRow(Base):
    __tablename__ = "agent_response_reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_decisions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING")
    proposed_response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    edited_response: Mapped[str | None] = mapped_column(Text)
    effective_response: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_type: Mapped[str] = mapped_column(String(80), nullable=False, default="operator")
    reviewer_reference: Mapped[str | None] = mapped_column(String(120))
    review_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("agent_decision_id", name="uq_agent_response_reviews_decision"),
    )


class AgentExecutionIntentRow(Base):
    __tablename__ = "agent_execution_intents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_decisions.id"), nullable=False)
    response_review_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_response_reviews.id"))
    autonomy_evaluation_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("autonomy_evaluations.id"))
    authorization_source: Mapped[str] = mapped_column(String(40), nullable=False, default="HUMAN_REVIEW")
    intent_type: Mapped[str] = mapped_column(String(80), nullable=False)
    effective_response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING")
    execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_delivery_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(160))
    release_status: Mapped[str] = mapped_column(String(40), nullable=False, default="HELD")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(120))
    recipient_reference: Mapped[str | None] = mapped_column(String(180))
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    capability_name: Mapped[str | None] = mapped_column(String(160))
    capability_request: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    provider_instance_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("provider_instances.id"))
    canonical_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("canonical_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("execution_intents.id"))
    execution_intent_fingerprint: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("response_review_id", name="uq_agent_execution_intents_review"),
        UniqueConstraint("autonomy_evaluation_id", name="uq_agent_execution_intents_autonomy_evaluation"),
        UniqueConstraint("idempotency_key", name="uq_agent_execution_intents_idempotency"),
        Index("uq_agent_execution_intents_production_parent", "execution_intent_id", unique=True, postgresql_where=text("execution_intent_id is not null"), sqlite_where=text("execution_intent_id is not null")),
        CheckConstraint("(execution_intent_id is null and execution_intent_fingerprint is null) or (execution_intent_id is not null and execution_intent_fingerprint is not null)", name="ck_agent_execution_intent_bridge_pair"),
    )


class LabConversationSessionRow(Base):
    __tablename__ = "lab_conversation_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_binding_id: Mapped[str] = mapped_column(String(64), ForeignKey("actor_bindings.id"), nullable=False)
    target_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_inbounds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_send_permits: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_permit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_slot: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)


class LabConversationInboundRow(Base):
    __tablename__ = "lab_conversation_inbounds"
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("lab_conversation_sessions.id"), nullable=False)
    inbound_event_id: Mapped[str] = mapped_column(String(64), ForeignKey("inbound_events.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("session_id", "ordinal", name="uq_lab_inbound_session_ordinal"),)


class LabDeliveryPermitRow(Base):
    __tablename__ = "lab_conversation_delivery_permits"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("lab_conversation_sessions.id"), nullable=False)
    review_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_response_reviews.id"), unique=True, nullable=False)
    target_binding_id: Mapped[str] = mapped_column(String(64), ForeignKey("actor_bindings.id"), nullable=False)
    permit_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_execution_intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_execution_intents.id"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryActorRow(Base):
    __tablename__ = "memory_actors"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "actor_key", name="uq_memory_actor_tenant_key"),)


class ConversationThreadRow(Base):
    __tablename__ = "conversation_threads"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False, default="default")
    external_thread_key: Mapped[str] = mapped_column(String(240), nullable=False)
    thread_type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(240))
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "source_account", "external_thread_key",
            name="uq_memory_thread_tenant_external",
        ),
    )


class ConversationParticipantRow(Base):
    __tablename__ = "conversation_participants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_threads.id"), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("memory_actors.id"))
    external_participant_key: Mapped[str] = mapped_column(String(240), nullable=False)
    observed_display_name: Mapped[str | None] = mapped_column(String(240))
    participant_role: Mapped[str] = mapped_column(String(40), nullable=False, default="MEMBER")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("conversation_id", "external_participant_key", name="uq_memory_participant"),)


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_threads.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_account: Mapped[str] = mapped_column(String(180), nullable=False, default="default")
    source_message_id: Mapped[str] = mapped_column(String(240), nullable=False)
    sender_actor_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("memory_actors.id"))
    external_sender_key: Mapped[str | None] = mapped_column(String(240))
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    from_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False, default="NORMAL")
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("conversation_messages.id"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "source_account", "source_message_id",
            name="uq_memory_message_tenant_external",
        ),
        Index("ix_memory_message_conversation_sent", "conversation_id", "sent_at"),
        Index("ix_memory_message_hash", "content_hash"),
    )


class MemoryExtractionRunRow(Base):
    __tablename__ = "memory_extraction_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id"), nullable=False, default=DEFAULT_TENANT_ID,
        server_default=DEFAULT_TENANT_ID,
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_messages.id"), nullable=False)
    extraction_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_extraction_runs.id"), nullable=False)
    subject: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    predicate: Mapped[str] = mapped_column(String(160), nullable=False)
    object: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    eligibility: Mapped[str] = mapped_column(String(40), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryClaimRow(Base):
    __tablename__ = "memory_claims"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_actor_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("memory_actors.id"))
    subject_entity_id: Mapped[str | None] = mapped_column(String(64))
    predicate: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    object_text: Mapped[str | None] = mapped_column(Text)
    object_actor_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("memory_actors.id"))
    object_entity_id: Mapped[str | None] = mapped_column(String(64))
    object_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    context: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sensitivity_class: Mapped[str] = mapped_column(String(20), nullable=False)
    source_quality: Mapped[str] = mapped_column(String(40), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    staleness_class: Mapped[str] = mapped_column(String(30), nullable=False, default="STABLE")
    supersedes_claim_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("memory_claims.id"))
    conflict_group_id: Mapped[str | None] = mapped_column(String(64))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_memory_claim_subject_predicate", "subject_actor_id", "predicate"),)


class MemoryEvidenceRow(Base):
    __tablename__ = "memory_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_claims.id"), nullable=False)
    conversation_message_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_messages.id"), nullable=False)
    extraction_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_extraction_runs.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(60), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryIngestionJobRow(Base):
    __tablename__ = "memory_ingestion_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_message_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversation_messages.id"), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantRow(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceRow(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "resource_type", "canonical_name", name="uq_resource_tenant_type_name"),
        Index("ix_resources_tenant_type", "tenant_id", "resource_type"),
    )


class RelationshipRow(Base):
    __tablename__ = "relationships"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    source_entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_relationship_source", "tenant_id", "source_entity_type", "source_entity_id"),
        Index("ix_relationship_target", "tenant_id", "target_entity_type", "target_entity_id"),
    )


class CanonicalEventRow(Base):
    __tablename__ = "canonical_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120))
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    channel: Mapped[str | None] = mapped_column(String(80))
    payload_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_ref: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    inbound_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("inbound_events.id"), unique=True)
    metadata_sanitized: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    lineage_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="HISTORICAL_UNKNOWN",
        server_default="HISTORICAL_UNKNOWN",
    )
    scenario_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_runs.id",
            name="fk_canonical_events_scenario_run",
            use_alter=True,
        ),
    )
    scenario_step_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_step_runs.id",
            name="fk_canonical_events_scenario_step_run",
            use_alter=True,
        ),
    )
    __table_args__ = (
        Index("ix_canonical_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_canonical_events_scenario_run", "tenant_id", "scenario_run_id"),
        CheckConstraint(
            "lineage_classification in "
            "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
            name="ck_canonical_event_lineage_classification",
        ),
    )


class TimelineEventRow(Base):
    __tablename__ = "timeline_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    canonical_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("canonical_events.id"))
    actor_id: Mapped[str | None] = mapped_column(String(120))
    relationship_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("relationships.id"))
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    event_ref: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(40), nullable=False, default="PRIVATE")
    provenance: Mapped[str] = mapped_column(String(80), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    __table_args__ = (
        Index("ix_timeline_tenant_actor_occurred", "tenant_id", "actor_id", "occurred_at"),
        Index("ix_timeline_tenant_resource_occurred", "tenant_id", "resource_id", "occurred_at"),
    )


class FactRow(Base):
    __tablename__ = "facts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    predicate: Mapped[str] = mapped_column(String(160), nullable=False)
    value_json: Mapped[dict[str, Any] | None] = mapped_column("value", JsonType)
    value_ref: Mapped[str | None] = mapped_column(String(240))
    fact_class: Mapped[str] = mapped_column(String(40), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(160))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_fact_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("facts.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_facts_tenant_subject_predicate", "tenant_id", "subject_type", "subject_id", "predicate"),)


class EntityStateRow(Base):
    __tablename__ = "entity_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    state_namespace: Mapped[str] = mapped_column(String(80), nullable=False)
    state_key: Mapped[str] = mapped_column(String(120), nullable=False)
    state_value: Mapped[dict[str, Any]] = mapped_column("value", JsonType, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "subject_type", "subject_id", "state_namespace", "state_key",
            name="uq_entity_state_subject_key",
        ),
    )


class StandingDirectiveRow(Base):
    """Tenant-scoped, auditable instruction distinct from operational state."""
    __tablename__ = "standing_directives"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    subject_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_type: Mapped[str] = mapped_column(String(96), nullable=False)
    audience_selector: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("status in ('ACTIVE','REVOKED')", name="ck_standing_directive_status"),
        CheckConstraint("version > 0", name="ck_standing_directive_version"),
        Index("ix_standing_directive_resolution", "tenant_id", "subject_actor_id", "trigger_type", "status"),
    )


class CapabilityDefinitionRow(Base):
    __tablename__ = "capability_definitions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    domain: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    availability_state: Mapped[str] = mapped_column(String(40), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "capability_versions.id",
            name="fk_capability_current_version",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "canonical_name", name="uq_capability_tenant_name"),
        Index("ix_capabilities_tenant_state", "tenant_id", "availability_state"),
    )


class CapabilityVersionRow(Base):
    __tablename__ = "capability_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(64), ForeignKey("capability_definitions.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    required_permissions: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    required_provider_interface: Mapped[str | None] = mapped_column(String(120))
    sensitivity: Mapped[str] = mapped_column(String(40), nullable=False)
    side_effect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    default_approval_policy: Mapped[str] = mapped_column(String(40), nullable=False)
    availability_state: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("capability_id", "version", name="uq_capability_version"),
        UniqueConstraint("capability_id", "manifest_checksum", name="uq_capability_checksum"),
    )


class ProviderDefinitionRow(Base):
    __tablename__ = "provider_definitions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    interface_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_provider_definitions_interface", "interface_name"),)


class ProviderInstanceRow(Base):
    __tablename__ = "provider_instances"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    provider_definition_id: Mapped[str] = mapped_column(String(64), ForeignKey("provider_definitions.id"), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    health: Mapped[str] = mapped_column(String(40), nullable=False)
    config_reference: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "canonical_name", name="uq_provider_instance_tenant_name"),)


class ProviderBindingRow(Base):
    __tablename__ = "provider_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(64), ForeignKey("capability_definitions.id"), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    provider_instance_id: Mapped[str] = mapped_column(String(64), ForeignKey("provider_instances.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_provider_bindings_resolution", "tenant_id", "capability_id", "resource_id", "status", "priority"),
    )


class CapabilityGrantRow(Base):
    __tablename__ = "capability_grants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    grantor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    grantor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    grantee_type: Mapped[str] = mapped_column(String(40), nullable=False)
    grantee_id: Mapped[str] = mapped_column(String(120), nullable=False)
    capability_id: Mapped[str] = mapped_column(String(64), ForeignKey("capability_definitions.id"), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    target_resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    constraints_json: Mapped[dict[str, Any]] = mapped_column("constraints", JsonType, nullable=False, default=dict)
    provenance: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(120))
    revocation_reason: Mapped[str | None] = mapped_column(String(240))
    __table_args__ = (
        Index("ix_capability_grants_resolution", "tenant_id", "grantee_type", "grantee_id", "capability_id", "status"),
    )


class CommitmentRow(Base):
    __tablename__ = "commitments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    created_by_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    responsible_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    beneficiary_actor_id: Mapped[str | None] = mapped_column(String(120))
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    summary: Mapped[str] = mapped_column(String(320), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="OPEN")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("canonical_events.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_commitments_tenant_responsible_status", "tenant_id", "responsible_actor_id", "status"),
        Index("ix_commitments_tenant_beneficiary_status", "tenant_id", "beneficiary_actor_id", "status"),
    )


class ReminderRow(Base):
    __tablename__ = "reminders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    owner_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    summary: Mapped[str] = mapped_column(String(320), nullable=False)
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SCHEDULED")
    source_event_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("canonical_events.id"))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_reminder_tenant_idempotency"),
        Index("ix_reminders_due_claim", "status", "trigger_at", "claimed_at"),
        Index("ix_reminders_tenant_owner_status", "tenant_id", "owner_actor_id", "status"),
    )


class DeviceRow(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "canonical_name", name="uq_device_tenant_name"),)


class DeviceIdentityRow(Base):
    __tablename__ = "device_identities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), nullable=False)
    identity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    identity_reference_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "identity_type", "identity_reference_hash", name="uq_device_identity_tenant_ref"),
    )


class DeviceBindingRow(Base):
    __tablename__ = "device_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), nullable=False)
    actor_key: Mapped[str | None] = mapped_column(String(120))
    resource_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("resources.id"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("actor_key is not null or resource_id is not null", name="ck_device_binding_target"),
        Index("ix_device_bindings_tenant_device", "tenant_id", "device_id"),
    )


class DeviceCapabilityRow(Base):
    __tablename__ = "device_capabilities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), nullable=False)
    capability_name: Mapped[str] = mapped_column(String(160), nullable=False)
    availability: Mapped[str] = mapped_column(String(40), nullable=False)
    announced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JsonType, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("tenant_id", "device_id", "capability_name", name="uq_device_capability_announcement"),
    )


class DeviceStatusRow(Base):
    __tablename__ = "device_statuses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), nullable=False)
    health: Mapped[str] = mapped_column(String(40), nullable=False)
    connectivity: Mapped[str] = mapped_column(String(40), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metrics_sanitized: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    __table_args__ = (Index("ix_device_status_device_observed", "tenant_id", "device_id", "observed_at"),)


class DependencyDefinitionRow(Base):
    __tablename__ = "dependency_definitions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    dependency_type: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_module: Mapped[str] = mapped_column(String(160), nullable=False)
    criticality: Mapped[str] = mapped_column(String(40), nullable=False)
    authoritative_source: Mapped[str] = mapped_column(String(120), nullable=False)
    health_source: Mapped[str | None] = mapped_column(String(160))
    freshness_source: Mapped[str | None] = mapped_column(String(160))
    sanitized_metadata: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    provenance_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_revision: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canonical_key", name="uq_dependency_definition_tenant_key"
        ),
        Index(
            "ix_dependency_definition_owner_type_active",
            "tenant_id",
            "owner_module",
            "dependency_type",
            "is_active",
        ),
    )


class DependencyEdgeRow(Base):
    __tablename__ = "dependency_edges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    upstream_dependency_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dependency_definitions.id"), nullable=False
    )
    downstream_dependency_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("dependency_definitions.id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "upstream_dependency_id",
            "downstream_dependency_id",
            "relation_type",
            name="uq_dependency_edge_tenant_relation",
        ),
        CheckConstraint(
            "upstream_dependency_id <> downstream_dependency_id",
            name="ck_dependency_edge_not_self",
        ),
        CheckConstraint(
            "relation_type in ('MANDATORY','ADVISORY')",
            name="ck_dependency_edge_relation_type",
        ),
        Index("ix_dependency_edge_upstream", "tenant_id", "upstream_dependency_id"),
        Index("ix_dependency_edge_downstream", "tenant_id", "downstream_dependency_id"),
    )


class OperationalObservationRow(Base):
    __tablename__ = "operational_observations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    freshness_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dependency_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("dependency_definitions.id")
    )
    component_key: Mapped[str | None] = mapped_column(String(160))
    lineage_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="HISTORICAL_UNKNOWN"
    )
    scenario_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_runs.id",
            name="fk_operational_observations_scenario_run",
            use_alter=True,
        ),
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    sanitized_metadata: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    source_revision: Mapped[str | None] = mapped_column(String(128))
    runtime_revision: Mapped[str | None] = mapped_column(String(128))
    schema_revision: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "dependency_id is not null or component_key is not null",
            name="ck_operational_observation_subject",
        ),
        CheckConstraint(
            "lineage_classification in "
            "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC')",
            name="ck_operational_observation_lineage",
        ),
        Index(
            "ix_operational_observation_subject_time",
            "tenant_id",
            "component_key",
            "observed_at",
        ),
        Index(
            "ix_operational_observation_dependency_time",
            "tenant_id",
            "dependency_id",
            "observed_at",
        ),
        Index("ix_operational_observation_correlation", "tenant_id", "correlation_id"),
    )


class ReadinessResultRow(Base):
    __tablename__ = "readiness_results"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    dimension: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_fresh_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_references: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    blocker_references: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    required_dependency_ids: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "dimension in ('COMPONENT_HEALTH','DOMAIN_READINESS','EVIDENCE_READINESS')",
            name="ck_readiness_result_dimension",
        ),
        CheckConstraint(
            "state in ('READY','DEGRADED','BLOCKED','UNKNOWN','STALE','NOT_READY')",
            name="ck_readiness_result_state",
        ),
        Index(
            "ix_readiness_subject_evaluated",
            "tenant_id",
            "dimension",
            "subject_type",
            "subject_key",
            "evaluated_at",
        ),
        Index(
            "uq_readiness_current_subject",
            "tenant_id",
            "dimension",
            "subject_type",
            "subject_key",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )


class FindingRow(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    active_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lineage_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_reference: Mapped[str | None] = mapped_column(String(120))
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_scope: Mapped[str | None] = mapped_column(String(160))
    suppression_reason: Mapped[str | None] = mapped_column(String(320))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_reason: Mapped[str | None] = mapped_column(String(320))
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("fingerprint_version > 0", name="ck_finding_fingerprint_version"),
        CheckConstraint("occurrence_count > 0", name="ck_finding_occurrence_count"),
        CheckConstraint("last_seen_at >= first_seen_at", name="ck_finding_seen_order"),
        CheckConstraint(
            "status in ('NEW','ACTIVE','ACKNOWLEDGED','RESOLVED','SUPPRESSED','EXPECTED')",
            name="ck_finding_status",
        ),
        CheckConstraint(
            "severity in ('CRITICAL','HIGH','MEDIUM','LOW')",
            name="ck_finding_severity",
        ),
        CheckConstraint(
            "lineage_classification in "
            "('HISTORICAL_UNKNOWN','UNKNOWN','ORGANIC','SYNTHETIC','MIXED')",
            name="ck_finding_lineage_classification",
        ),
        Index(
            "ix_findings_tenant_status_severity_seen",
            "tenant_id",
            "status",
            "severity",
            "last_seen_at",
        ),
        Index(
            "uq_findings_active_fingerprint",
            "tenant_id",
            "fingerprint_version",
            "fingerprint",
            "active_scope",
            unique=True,
            postgresql_where=text("status in ('NEW','ACTIVE','ACKNOWLEDGED')"),
            sqlite_where=text("status in ('NEW','ACTIVE','ACKNOWLEDGED')"),
        ),
    )


class FindingOccurrenceRow(Base):
    __tablename__ = "finding_occurrences"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    finding_id: Mapped[str] = mapped_column(String(64), ForeignKey("findings.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    component_key: Mapped[str] = mapped_column(String(160), nullable=False)
    scenario_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "scenario_runs.id",
            name="fk_finding_occurrences_scenario_run",
            use_alter=True,
        ),
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(120), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_finding_occurrence_finding_time", "finding_id", "observed_at"),
        Index("ix_finding_occurrence_correlation", "tenant_id", "correlation_id"),
    )


class EvidenceReferenceRow(Base):
    __tablename__ = "evidence_references"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("findings.id"))
    finding_occurrence_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("finding_occurrences.id")
    )
    internal_entity_type: Mapped[str | None] = mapped_column(String(80))
    internal_entity_id: Mapped[str | None] = mapped_column(String(128))
    external_reference: Mapped[str | None] = mapped_column(String(320))
    artifact_reference: Mapped[str | None] = mapped_column(String(320))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    source_sha: Mapped[str | None] = mapped_column(String(128))
    sanitized_metadata: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "internal_entity_id is not null or external_reference is not null "
            "or artifact_reference is not null or trace_id is not null "
            "or finding_id is not null or finding_occurrence_id is not null",
            name="ck_evidence_reference_target",
        ),
        CheckConstraint(
            "(internal_entity_type is null) = (internal_entity_id is null)",
            name="ck_evidence_reference_internal_pair",
        ),
        Index(
            "ix_evidence_reference_internal",
            "tenant_id",
            "internal_entity_type",
            "internal_entity_id",
        ),
        Index("ix_evidence_reference_trace_source", "tenant_id", "trace_id", "source_sha"),
    )


class ScenarioDefinitionRow(Base):
    __tablename__ = "scenario_definitions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_key: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "scenario_key", name="uq_scenario_definition_tenant_key"),
    )


class ScenarioVersionRow(Base):
    __tablename__ = "scenario_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_definition_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_definitions.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest_source_path: Mapped[str] = mapped_column(String(320), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    requirements_covered: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    risk_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    config_keys: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    risk_classification: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    environment_classification: Mapped[str | None] = mapped_column(String(24))
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_scenario_version_positive"),
        CheckConstraint("is_immutable", name="ck_scenario_version_immutable"),
        UniqueConstraint(
            "scenario_definition_id", "version", name="uq_scenario_version_definition_version"
        ),
        UniqueConstraint(
            "scenario_definition_id", "content_hash", name="uq_scenario_version_definition_hash"
        ),
    )


class ExecutionClassVersionRow(Base):
    __tablename__ = "execution_class_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_classification: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    allowed_transports: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    allowed_operations: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    allowed_capabilities: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    external_effect_class: Mapped[str] = mapped_column(String(80), nullable=False)
    max_target_cardinality: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_human_authorization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_fresh_readiness: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_handoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_bounded_authorization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    direct_execution_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("identity", "version", name="uq_execution_class_version_identity"),
        CheckConstraint("version > 0", name="ck_execution_class_version_positive"),
        CheckConstraint("max_target_cardinality >= 0", name="ck_execution_class_target_cardinality"),
        CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_execution_class_environment"),
        CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_execution_class_status"),
    )


class SafetySetVersionRow(Base):
    __tablename__ = "safety_set_versions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_classification: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    execution_class_version_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_class_versions.id"), nullable=False)
    allowed_transport: Mapped[str] = mapped_column(String(80), nullable=False)
    allowed_operation: Mapped[str] = mapped_column(String(120), nullable=False)
    allowed_capability: Mapped[str] = mapped_column(String(120), nullable=False)
    max_target_cardinality: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_outbound_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    required_invariants: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("identity", "version", name="uq_safety_set_version_identity"),
        CheckConstraint("version > 0", name="ck_safety_set_version_positive"),
        CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_safety_set_environment"),
        CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_safety_set_status"),
        CheckConstraint("max_target_cardinality >= 0 and max_outbound_messages >= 0 and max_action_count >= 0", name="ck_safety_set_ceilings"),
    )


class StaticIntentAuthorityProfileRow(Base):
    __tablename__ = "static_intent_authority_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment_classification: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    max_target_cardinality: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_outbound_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allowed_transport: Mapped[str] = mapped_column(String(80), nullable=False)
    allowed_operation: Mapped[str] = mapped_column(String(120), nullable=False)
    allowed_capability: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        UniqueConstraint("identity", "version", name="uq_intent_authority_profile_identity"),
        CheckConstraint("version > 0", name="ck_intent_authority_profile_positive"),
        CheckConstraint("environment_classification in ('PRODUCTION','SYNTHETIC','TEST_ONLY')", name="ck_intent_authority_profile_environment"),
        CheckConstraint("status in ('ACTIVE','DEPRECATED','RETIRED')", name="ck_intent_authority_profile_status"),
        CheckConstraint("max_target_cardinality >= 0 and max_outbound_messages >= 0 and max_action_count >= 0 and max_retries >= 0", name="ck_intent_authority_profile_limits"),
    )


class RecipientEndpointRow(Base):
    __tablename__ = "recipient_endpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    transport: Mapped[str] = mapped_column(String(80), nullable=False)
    canonical_address: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "transport", "canonical_address", name="uq_recipient_endpoint_identity"),
        CheckConstraint("status in ('ACTIVE','INACTIVE','RETIRED')", name="ck_recipient_endpoint_status"),
    )


class ScenarioVersionAuthorityBindingRow(Base):
    __tablename__ = "scenario_version_authority_bindings"
    scenario_version_id: Mapped[str] = mapped_column(String(64), ForeignKey("scenario_versions.id"), primary_key=True)
    binding_role: Mapped[str] = mapped_column(String(32), primary_key=True)
    execution_class_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("execution_class_versions.id"))
    safety_set_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("safety_set_versions.id"))
    policy_version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("policy_versions.id"))
    authority_profile_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("static_intent_authority_profiles.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("binding_role in ('EXECUTION_CLASS','SAFETY_SET','POLICY','AUTHORITY_PROFILE')", name="ck_scenario_authority_binding_role"),)


class ExecutionIntentTargetBindingRow(Base):
    __tablename__ = "execution_intent_target_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_intent_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_intents.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_endpoint_id: Mapped[str] = mapped_column(String(64), ForeignKey("recipient_endpoints.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("execution_intent_id", "target_type", name="uq_intent_target_type"),
        UniqueConstraint("execution_intent_id", "ordinal", name="uq_intent_target_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_intent_target_ordinal"),
        CheckConstraint("target_type = 'WHATSAPP_RECIPIENT_ENDPOINT'", name="ck_intent_target_type"),
    )


class ScenarioRunRow(Base):
    __tablename__ = "scenario_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_versions.id"), nullable=False
    )
    synthetic_actor_binding_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("actor_bindings.id")
    )
    agent_execution_intent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_execution_intents.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    root_correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_sha: Mapped[str | None] = mapped_column(String(128))
    schema_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    driver_revision: Mapped[str | None] = mapped_column(String(128))
    readiness_result_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("readiness_results.id")
    )
    effect_budget_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "effect_budgets.id",
            name="fk_scenario_runs_effect_budget",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifying_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(320))
    cleanup_state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status in ('CREATED','VALIDATING','NOT_READY','ARMED','RUNNING',"
            "'VERIFYING','PASSED','FAILED','ABORTED','EXPIRED')",
            name="ck_scenario_run_status",
        ),
        CheckConstraint(
            "cleanup_state in ('PENDING','RUNNING','COMPLETE','FAILED','NOT_REQUIRED')",
            name="ck_scenario_run_cleanup_state",
        ),
        CheckConstraint(
            "status not in ('PASSED','FAILED','ABORTED','EXPIRED') "
            "or completed_at is not null",
            name="ck_scenario_run_terminal_completed",
        ),
        UniqueConstraint(
            "tenant_id", "root_correlation_id", name="uq_scenario_run_tenant_correlation"
        ),
        Index("ix_scenario_run_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_scenario_run_agent_execution_intent", "agent_execution_intent_id"),
        Index("uq_scenario_run_production_agent_intent", "agent_execution_intent_id", unique=True, postgresql_where=text("agent_execution_intent_id is not null"), sqlite_where=text("agent_execution_intent_id is not null")),
    )


class BoundedRunAuthorizationRow(Base):
    """Durable, least-privilege admission for one bounded external run."""

    __tablename__ = "bounded_run_authorizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("scenario_runs.id"), nullable=False)
    effect_budget_id: Mapped[str] = mapped_column(String(64), ForeignKey("effect_budgets.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_scope: Mapped[str] = mapped_column(String(180), nullable=False)
    target_scope: Mapped[str] = mapped_column(String(240), nullable=False)
    capability_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    effect_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    max_effects: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")
    authorized_by: Mapped[str] = mapped_column(String(120), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("status in ('ACTIVE','REVOKED','EXPIRED','CONSUMED')", name="ck_bounded_run_authorization_status"),
        CheckConstraint("max_effects > 0", name="ck_bounded_run_authorization_max_effects"),
        CheckConstraint("expires_at > valid_from", name="ck_bounded_run_authorization_validity"),
        CheckConstraint("authorized_by not in ('SYNTHETIC_ACTOR','ANDY','PROVIDER','TRANSPORT')", name="ck_bounded_run_authorization_authority"),
        UniqueConstraint("tenant_id", "scenario_run_id", "effect_budget_id", name="uq_bounded_run_authorization_run_budget"),
        Index("ix_bounded_run_authorization_lookup", "tenant_id", "scenario_run_id", "status"),
    )


class TransportCanaryAuthorizationRow(Base):
    """Immutable, single-use authorization boundary for a transport canary."""

    __tablename__ = "transport_canary_authorizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transport: Mapped[str] = mapped_column(String(80), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(String(80), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    recipient: Mapped[str] = mapped_column(String(160), nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PREPARED")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorized_by: Mapped[str | None] = mapped_column(String(120))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("status in ('PREPARED','AUTHORIZED','REVOKED','EXPIRED','CONSUMED')", name="ck_transport_canary_authorization_status"),
        CheckConstraint("expires_at > valid_from", name="ck_transport_canary_authorization_validity"),
        UniqueConstraint("scope_fingerprint", name="uq_transport_canary_authorization_fingerprint"),
    )


class ExecutionIntentRow(Base):
    """Inert semantic scope; no readiness, run, lease, or effect relationship."""
    __tablename__ = "execution_intents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    scope_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PREPARED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authority_profile_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("static_intent_authority_profiles.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HumanExecutionAuthorizationRow(Base):
    """Human approval for one immutable execution intent; not runtime admission."""
    __tablename__ = "human_execution_authorizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_intent_id: Mapped[str] = mapped_column(String(64), ForeignKey("execution_intents.id"), nullable=False)
    execution_intent_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    scope_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_approver: Mapped[str] = mapped_column(String(120), nullable=False)
    approval_channel: Mapped[str] = mapped_column(String(80), nullable=False)
    request_wamid: Mapped[str | None] = mapped_column(String(180))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PREPARED")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_inbound_wamid: Mapped[str | None] = mapped_column(String(180))
    decision_sender: Mapped[str | None] = mapped_column(String(120))
    decision_button_id: Mapped[str | None] = mapped_column(String(180))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("state in ('PREPARED','PENDING_HUMAN_APPROVAL','APPROVED','DENIED','EXPIRED','CONSUMED','REVOKED')", name="ck_human_execution_authorization_state"),
        CheckConstraint("expires_at > issued_at", name="ck_human_execution_authorization_validity"),
        UniqueConstraint("scope_fingerprint", name="uq_human_execution_authorization_fingerprint"),
        Index(
            "uq_human_execution_authorization_request_wamid",
            "request_wamid",
            unique=True,
            postgresql_where=text("request_wamid IS NOT NULL"),
            sqlite_where=text("request_wamid IS NOT NULL"),
        ),
    )


class HumanApprovalDeliveryEvidenceRow(Base):
    """Exact relational delivery evidence retained for historical HEA requests."""

    __tablename__ = "human_approval_delivery_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    authorization_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_execution_authorizations.id"),
        nullable=False,
    )
    inbox_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("meta_callback_inbox.id")
    )
    source_audit_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("audit_events.id")
    )
    deduplication_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_status: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "provider_status in ('sent','delivered','read','failed')",
            name="ck_human_approval_delivery_evidence_status",
        ),
        CheckConstraint(
            "(inbox_id is null) <> (source_audit_id is null)",
            name="ck_human_approval_delivery_evidence_origin",
        ),
        UniqueConstraint(
            "inbox_id", name="uq_human_approval_delivery_evidence_inbox"
        ),
        UniqueConstraint(
            "source_audit_id",
            name="uq_human_approval_delivery_evidence_source_audit",
        ),
        UniqueConstraint(
            "authorization_id",
            "deduplication_key",
            name="uq_human_approval_delivery_evidence_deduplication",
        ),
        Index(
            "ix_human_approval_delivery_evidence_authorization_received",
            "authorization_id",
            "received_at",
            "id",
        ),
    )


class ScenarioStepRunRow(Base):
    __tablename__ = "scenario_step_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_runs.id"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_lease_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "execution_leases.id",
            name="fk_scenario_step_runs_execution_lease",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(320))
    cleanup_state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    __table_args__ = (
        CheckConstraint("step_order >= 0", name="ck_scenario_step_order"),
        CheckConstraint(
            "attempt >= 0 and attempt <= max_attempts and max_attempts > 0",
            name="ck_scenario_step_attempts",
        ),
        CheckConstraint(
            "status in ('PENDING','RUNNING','VERIFYING','PASSED','FAILED','SKIPPED',"
            "'ABORTED','EXPIRED','RECONCILIATION_REQUIRED')",
            name="ck_scenario_step_status",
        ),
        CheckConstraint(
            "cleanup_state in ('PENDING','RUNNING','COMPLETE','FAILED','NOT_REQUIRED')",
            name="ck_scenario_step_cleanup_state",
        ),
        UniqueConstraint("scenario_run_id", "step_key", name="uq_scenario_step_run_key"),
        UniqueConstraint("scenario_run_id", "step_order", name="uq_scenario_step_run_order"),
        UniqueConstraint(
            "tenant_id", "scenario_run_id", "idempotency_key",
            name="uq_scenario_step_run_idempotency",
        ),
        Index("ix_scenario_step_run_status", "tenant_id", "scenario_run_id", "status"),
    )


class EffectBudgetRow(Base):
    __tablename__ = "effect_budgets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_runs.id"), nullable=False
    )
    effect_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_scope: Mapped[str] = mapped_column(String(240), nullable=False)
    stimulus_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    system_effect_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="AVAILABLE")
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "stimulus_limit >= 0 and system_effect_limit >= 0",
            name="ck_effect_budget_limits",
        ),
        CheckConstraint(
            "reserved_count >= 0 and consumed_count >= 0",
            name="ck_effect_budget_counts",
        ),
        CheckConstraint("valid_until > valid_from", name="ck_effect_budget_validity"),
        CheckConstraint(
            "status in ('AVAILABLE','RESERVED','CONSUMED','RELEASED','CANCELLED')",
            name="ck_effect_budget_status",
        ),
        CheckConstraint(
            "reserved_count <= stimulus_limit + system_effect_limit "
            "and consumed_count <= stimulus_limit + system_effect_limit",
            name="ck_effect_budget_capacity",
        ),
        CheckConstraint("version > 0", name="ck_effect_budget_version"),
        UniqueConstraint(
            "tenant_id", "scenario_run_id", "effect_type", "target_scope",
            name="uq_effect_budget_run_effect_target",
        ),
        Index("ix_effect_budget_run_status", "tenant_id", "scenario_run_id", "status"),
    )


class ExecutionLeaseRow(Base):
    __tablename__ = "execution_leases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    lease_type: Mapped[str] = mapped_column(String(80), nullable=False)
    purpose: Mapped[str] = mapped_column(String(160), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(180), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("scenario_runs.id"))
    scenario_step_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("scenario_step_runs.id")
    )
    claimant_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="AVAILABLE")
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_claims: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claimed_event_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("inbound_events.id")
    )
    logical_execution_id: Mapped[str | None] = mapped_column(String(120))
    effect_budget_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("effect_budgets.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "claim_count >= 0 and max_claims > 0 and claim_count <= max_claims",
            name="ck_execution_lease_claim_count",
        ),
        CheckConstraint("expires_at > not_before", name="ck_execution_lease_validity"),
        CheckConstraint(
            "status in ('AVAILABLE','CLAIMED','CONSUMED','EXPIRED','CANCELLED','FAILED')",
            name="ck_execution_lease_status",
        ),
        CheckConstraint("version > 0", name="ck_execution_lease_version"),
        UniqueConstraint(
            "tenant_id", "lease_type", "idempotency_key",
            name="uq_execution_lease_tenant_type_idempotency",
        ),
        Index(
            "uq_execution_lease_active_scope",
            "tenant_id",
            "lease_type",
            "scope_key",
            unique=True,
            postgresql_where=text("status in ('AVAILABLE','CLAIMED')"),
            sqlite_where=text("status in ('AVAILABLE','CLAIMED')"),
        ),
        Index(
            "ix_execution_lease_claim",
            "tenant_id",
            "lease_type",
            "status",
            "expires_at",
        ),
    )


class EffectConsumptionRow(Base):
    __tablename__ = "effect_consumptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    effect_budget_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("effect_budgets.id"), nullable=False
    )
    logical_effect_id: Mapped[str] = mapped_column(String(160), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    target_scope: Mapped[str] = mapped_column(String(240), nullable=False)
    execution_lease_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("execution_leases.id"), nullable=False
    )
    execution_intent_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_execution_intents.id")
    )
    outbox_message_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("outbox_messages.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="RESERVED")
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("direction in ('STIMULUS','SYSTEM')", name="ck_effect_consumption_direction"),
        CheckConstraint(
            "state in ('RESERVED','CONSUMED','RELEASED','CANCELLED')",
            name="ck_effect_consumption_state",
        ),
        UniqueConstraint(
            "effect_budget_id", "logical_effect_id",
            name="uq_effect_consumption_budget_logical_effect",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_effect_consumption_tenant_idempotency"
        ),
        Index(
            "uq_effect_consumption_execution_intent",
            "execution_intent_id",
            unique=True,
            postgresql_where=text("execution_intent_id is not null"),
            sqlite_where=text("execution_intent_id is not null"),
        ),
        Index(
            "uq_effect_consumption_outbox_message",
            "outbox_message_id",
            unique=True,
            postgresql_where=text("outbox_message_id is not null"),
            sqlite_where=text("outbox_message_id is not null"),
        ),
        Index("ix_effect_consumption_budget_state", "effect_budget_id", "state"),
    )


class AssertionResultRow(Base):
    __tablename__ = "assertion_results"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_runs.id"), nullable=False
    )
    scenario_step_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("scenario_step_runs.id")
    )
    assertion_id: Mapped[str] = mapped_column(String(120), nullable=False)
    assertion_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_property: Mapped[str] = mapped_column(Text, nullable=False)
    observed_summary: Mapped[str | None] = mapped_column(Text)
    evaluator: Mapped[str] = mapped_column(String(120), nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    evidence_reference_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    finding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("findings.id"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    __table_args__ = (
        CheckConstraint(
            "result in ('PASS','FAIL','UNKNOWN','NOT_EVALUATED')",
            name="ck_assertion_result_value",
        ),
        UniqueConstraint(
            "scenario_run_id", "assertion_id", "assertion_version", "attempt",
            name="uq_assertion_result_run_identity",
        ),
        Index("ix_assertion_result_run_result", "tenant_id", "scenario_run_id", "result"),
    )


class InvariantResultRow(Base):
    __tablename__ = "invariant_results"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    scenario_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenario_runs.id"), nullable=False
    )
    scenario_step_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("scenario_step_runs.id")
    )
    invariant_id: Mapped[str] = mapped_column(String(120), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    enforcement_owner: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    violation_reason: Mapped[str | None] = mapped_column(String(320))
    aborts_scenario: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocks_promotion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_reference_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    finding_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("findings.id"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    __table_args__ = (
        CheckConstraint(
            "result in ('PASS','FAIL','UNKNOWN','NOT_EVALUATED')",
            name="ck_invariant_result_value",
        ),
        UniqueConstraint(
            "scenario_run_id", "invariant_id", "attempt",
            name="uq_invariant_result_run_identity",
        ),
        Index("ix_invariant_result_run_result", "tenant_id", "scenario_run_id", "result"),
    )


class DiagnosisCandidateRow(Base):
    __tablename__ = "diagnosis_candidates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    finding_id: Mapped[str] = mapped_column(String(64), ForeignKey("findings.id"), nullable=False)
    engine: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    missing_information: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    suspected_component: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_diagnosis_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("diagnosis_candidates.id")
    )
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 and confidence <= 1", name="ck_diagnosis_candidate_confidence"
        ),
        Index("ix_diagnosis_candidate_finding_created", "tenant_id", "finding_id", "created_at"),
    )


class RemediationProposalRow(Base):
    __tablename__ = "remediation_proposals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    diagnosis_candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("diagnosis_candidates.id"), nullable=False
    )
    scope: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    affected_components: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    rollback_requirements: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    required_tests: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    required_invariants: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    required_evidence: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    author_type: Mapped[str] = mapped_column(String(40), nullable=False)
    author_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_proposal_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("remediation_proposals.id")
    )
    __table_args__ = (
        Index(
            "ix_remediation_proposal_diagnosis_created",
            "tenant_id",
            "diagnosis_candidate_id",
            "created_at",
        ),
    )


class PatchCandidateRow(Base):
    __tablename__ = "patch_candidates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    remediation_proposal_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("remediation_proposals.id")
    )
    base_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    branch_reference: Mapped[str] = mapped_column(String(240), nullable=False)
    worktree_reference: Mapped[str] = mapped_column(String(320), nullable=False)
    artifact_references: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("base_sha <> candidate_sha", name="ck_patch_candidate_distinct_sha"),
        UniqueConstraint("tenant_id", "candidate_sha", name="uq_patch_candidate_tenant_sha"),
        Index("ix_patch_candidate_status_created", "tenant_id", "status", "created_at"),
    )


class VerificationRunRow(Base):
    __tablename__ = "verification_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    patch_candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("patch_candidates.id"), nullable=False
    )
    candidate_sha_snapshot: Mapped[str] = mapped_column(String(128), nullable=False)
    environment_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    test_results: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    assertion_result_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    invariant_result_ids: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    differential_results: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    rollback_evidence: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    artifact_references: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    secret_scan_status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index(
            "ix_verification_run_candidate_created",
            "tenant_id",
            "patch_candidate_id",
            "created_at",
        ),
    )


class PromotionDecisionRow(Base):
    __tablename__ = "promotion_decisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    patch_candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("patch_candidates.id"), nullable=False
    )
    verification_run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("verification_runs.id")
    )
    promotion_request_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "promotion_decisions.id",
            name="fk_promotion_decision_request",
            use_alter=True,
        ),
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_type: Mapped[str] = mapped_column(String(40), nullable=False)
    authority_reference: Mapped[str | None] = mapped_column(String(120))
    evidence_coverage: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    blocking_finding_snapshot: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    risk_disposition: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    rollback_reference: Mapped[str | None] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    required_source_sha: Mapped[str] = mapped_column(String(128), nullable=False)
    required_runtime_sha: Mapped[str | None] = mapped_column(String(128))
    required_schema_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "decision in ('PROMOTION_REQUIRED','APPROVED','REJECTED','CANCELLED')",
            name="ck_promotion_decision_value",
        ),
        CheckConstraint(
            "decision <> 'APPROVED' or "
            "(authority_type in ('HUMAN_OWNER','HUMAN_OPERATOR') "
            "and authority_reference is not null)",
            name="ck_promotion_approval_human_authority",
        ),
        CheckConstraint(
            "(decision = 'PROMOTION_REQUIRED' and promotion_request_id is null) or "
            "(decision <> 'PROMOTION_REQUIRED' and promotion_request_id is not null)",
            name="ck_promotion_terminal_request_link",
        ),
        UniqueConstraint(
            "promotion_request_id",
            name="uq_promotion_decision_terminal_request",
        ),
        Index(
            "ix_promotion_decision_candidate_created",
            "tenant_id",
            "patch_candidate_id",
            "created_at",
        ),
    )
