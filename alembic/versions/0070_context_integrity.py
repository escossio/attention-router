"""Guard contextual continuity against legacy PostgreSQL row writes."""

from alembic import op
import sqlalchemy as sa

revision = "0070_context_integrity"
down_revision = "0069_cognitive_revocation_v0"
branch_labels = None
depends_on = None
REVISION_TABLES = (
    "actor_bindings", "client_tenant_memberships", "artifacts", "artifact_receipts", "conversation_messages",
)


def upgrade():
    for table in REVISION_TABLES:
        op.add_column(table, sa.Column(
            "context_revision", sa.String(36), nullable=False, server_default="legacy",
        ))

    op.execute("""
      CREATE FUNCTION context_binding_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE old_counts jsonb; new_counts jsonb;
      BEGIN
        old_counts := coalesce((OLD.metadata::jsonb)->'context_generations', '{}'::jsonb);
        IF TG_OP = 'DELETE' THEN
          IF old_counts <> '{}'::jsonb THEN
            RAISE EXCEPTION 'CONTEXT_GENERATION_REGRESSION' USING ERRCODE = '23514';
          END IF;
          RETURN OLD;
        END IF;
        new_counts := coalesce((NEW.metadata::jsonb)->'context_generations', '{}'::jsonb);
        IF jsonb_typeof(old_counts) <> 'object' OR jsonb_typeof(new_counts) <> 'object' THEN
          RAISE EXCEPTION 'CONTEXT_GENERATION_REGRESSION' USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
          SELECT 1 FROM jsonb_each_text(old_counts) AS entry(scope, counter)
          WHERE NOT (new_counts ? scope)
             OR CASE WHEN (new_counts->>scope) ~ '^[0-9]+$' AND counter ~ '^[0-9]+$'
                     THEN (new_counts->>scope)::numeric < counter::numeric
                     ELSE true END
        ) THEN
          RAISE EXCEPTION 'CONTEXT_GENERATION_REGRESSION' USING ERRCODE = '23514';
        END IF;
        NEW.context_revision := OLD.context_revision;
        IF (NEW.id, NEW.tenant_id, NEW.source, NEW.external_actor_id, NEW.actor_key,
            NEW.actor_category, NEW.is_active, (NEW.metadata::jsonb)->'owner')
           IS DISTINCT FROM
           (OLD.id, OLD.tenant_id, OLD.source, OLD.external_actor_id, OLD.actor_key,
            OLD.actor_category, OLD.is_active, (OLD.metadata::jsonb)->'owner') THEN
          NEW.context_revision := gen_random_uuid()::text;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER context_binding_integrity
        BEFORE UPDATE OR DELETE ON actor_bindings
        FOR EACH ROW EXECUTE FUNCTION context_binding_integrity()
    """)
    op.execute("""
      CREATE FUNCTION context_membership_revision() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'INSERT' THEN
          NEW.context_revision := gen_random_uuid()::text;
        ELSIF (NEW.id, NEW.tenant_id, NEW.human_identity_id, NEW.role, NEW.status)
           IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.human_identity_id, OLD.role, OLD.status) THEN
          NEW.context_revision := gen_random_uuid()::text;
        ELSE
          NEW.context_revision := OLD.context_revision;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER context_membership_revision
        BEFORE INSERT OR UPDATE ON client_tenant_memberships
        FOR EACH ROW EXECUTE FUNCTION context_membership_revision()
    """)
    op.execute("""
      CREATE FUNCTION context_artifact_revision() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'INSERT' THEN
          NEW.context_revision := gen_random_uuid()::text;
        ELSIF (NEW.tenant_id, NEW.resource_id, NEW.content_sha256, NEW.artifact_kind, NEW.status)
           IS DISTINCT FROM (OLD.tenant_id, OLD.resource_id, OLD.content_sha256, OLD.artifact_kind, OLD.status) THEN
          NEW.context_revision := gen_random_uuid()::text;
        ELSE
          NEW.context_revision := OLD.context_revision;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER context_artifact_revision BEFORE INSERT OR UPDATE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION context_artifact_revision()
    """)
    op.execute("""
      CREATE FUNCTION context_journal_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        RAISE EXCEPTION 'CONTEXT_JOURNAL_IMMUTABLE' USING ERRCODE = '23514';
      END $$;
      CREATE TRIGGER context_journal_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW WHEN (OLD.origin = 'context_validity')
        EXECUTE FUNCTION context_journal_immutable()
    """)
    op.execute("""
      CREATE FUNCTION context_tombstone_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_TABLE_NAME = 'conversation_messages'
           AND (NEW.metadata::jsonb)->'context_evidence_withdrawn' = 'true'::jsonb THEN
          NEW.searchable := false;
        END IF;
        IF TG_OP = 'INSERT' THEN
          NEW.context_revision := gen_random_uuid()::text;
          RETURN NEW;
        END IF;
        IF (OLD.metadata::jsonb)->TG_ARGV[0] = 'true'::jsonb
           AND (NEW.metadata::jsonb)->TG_ARGV[0] IS DISTINCT FROM 'true'::jsonb THEN
          RAISE EXCEPTION 'CONTEXT_TOMBSTONE_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
        IF TG_TABLE_NAME = 'timeline_events'
           AND (OLD.metadata::jsonb) ? 'observation_scope'
           AND (NEW.metadata::jsonb)->'observation_scope'
               IS DISTINCT FROM (OLD.metadata::jsonb)->'observation_scope' THEN
          RAISE EXCEPTION 'CONTEXT_OBSERVATION_SCOPE_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
        IF TG_TABLE_NAME = 'artifact_receipts' AND (OLD.metadata::jsonb) ? 'context_expires_at'
           AND (NOT ((NEW.metadata::jsonb) ? 'context_expires_at')
                OR ((NEW.metadata::jsonb)->>'context_expires_at')::timestamptz IS NULL
                OR ((NEW.metadata::jsonb)->>'context_expires_at')::timestamptz
                   > ((OLD.metadata::jsonb)->>'context_expires_at')::timestamptz) THEN
          RAISE EXCEPTION 'CONTEXT_RECEIPT_DEADLINE_IMMUTABLE' USING ERRCODE = '23514';
        END IF;
        IF TG_TABLE_NAME = 'conversation_messages' THEN
          NEW.context_revision := OLD.context_revision;
          IF (NEW.id, NEW.tenant_id, NEW.conversation_id, NEW.source, NEW.source_account,
              NEW.source_message_id, NEW.sent_at, NEW.direction, NEW.from_me, NEW.message_type,
              NEW.searchable, NEW.sensitivity_class,
              (NEW.metadata::jsonb)->'capture_contract',
              (NEW.metadata::jsonb)#>'{platform_lineage,classification}',
              (NEW.metadata::jsonb)->'attachment_state', (NEW.metadata::jsonb)->'source_inbound_event_id',
              (NEW.metadata::jsonb)->'artifact_receipt_id', (NEW.metadata::jsonb)->'artifact_id')
             IS DISTINCT FROM
             (OLD.id, OLD.tenant_id, OLD.conversation_id, OLD.source, OLD.source_account,
              OLD.source_message_id, OLD.sent_at, OLD.direction, OLD.from_me, OLD.message_type,
              OLD.searchable, OLD.sensitivity_class,
              (OLD.metadata::jsonb)->'capture_contract',
              (OLD.metadata::jsonb)#>'{platform_lineage,classification}',
              (OLD.metadata::jsonb)->'attachment_state', (OLD.metadata::jsonb)->'source_inbound_event_id',
              (OLD.metadata::jsonb)->'artifact_receipt_id', (OLD.metadata::jsonb)->'artifact_id') THEN
            NEW.context_revision := gen_random_uuid()::text;
          END IF;
        ELSIF TG_TABLE_NAME = 'artifact_receipts' THEN
          NEW.context_revision := OLD.context_revision;
          IF (NEW.id, NEW.tenant_id, NEW.artifact_id, NEW.source_channel, NEW.source_account, NEW.external_receipt_id)
             IS DISTINCT FROM
             (OLD.id, OLD.tenant_id, OLD.artifact_id, OLD.source_channel, OLD.source_account, OLD.external_receipt_id) THEN
            NEW.context_revision := gen_random_uuid()::text;
          END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER context_message_tombstone BEFORE INSERT OR UPDATE ON conversation_messages
        FOR EACH ROW EXECUTE FUNCTION context_tombstone_integrity('context_evidence_withdrawn');
      CREATE TRIGGER context_event_tombstone BEFORE UPDATE ON timeline_events
        FOR EACH ROW EXECUTE FUNCTION context_tombstone_integrity('technical_evidence_revoked');
      CREATE TRIGGER context_receipt_tombstone BEFORE INSERT OR UPDATE ON artifact_receipts
        FOR EACH ROW EXECUTE FUNCTION context_tombstone_integrity('context_evidence_withdrawn')
    """)

    op.execute("""
      CREATE FUNCTION context_derivative_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.evidence_status = 'EVIDENCE_REVOKED' AND (
           NEW.evidence_status <> 'EVIDENCE_REVOKED'
           OR (TG_TABLE_NAME = 'candidate_insights' AND NEW.state = 'ADMITTED'
               AND OLD.state <> 'ADMITTED')) THEN
          RAISE EXCEPTION 'CONTEXT_DERIVATIVE_ALREADY_REVOKED' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER context_candidate_technical_status BEFORE UPDATE ON candidate_insights
        FOR EACH ROW EXECUTE FUNCTION context_derivative_integrity();
      CREATE TRIGGER context_episode_technical_status BEFORE UPDATE ON semantic_episodes
        FOR EACH ROW EXECUTE FUNCTION context_derivative_integrity()
    """)


def downgrade():
    # Hold writers from the history checks through removal of their guards.
    op.execute("""
        LOCK TABLE actor_bindings, artifact_receipts, artifacts, audit_events,
            candidate_insights, client_tenant_memberships, conversation_messages,
            semantic_episodes, timeline_events IN ACCESS EXCLUSIVE MODE
    """)
    bind = op.get_bind()
    if bind.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM candidate_insights WHERE evidence_status <> 'CURRENT'
                       UNION ALL SELECT 1 FROM semantic_episodes WHERE evidence_status <> 'CURRENT')
    """)):
        raise RuntimeError("COGNITIVE_REVOCATION_DOWNGRADE_REQUIRES_NO_REVOKED_ROWS")
    if bind.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM timeline_events WHERE metadata::jsonb ? 'owner_authority_revision'
                       UNION ALL SELECT 1 FROM audit_events WHERE origin = 'context_validity'
                       UNION ALL SELECT 1 FROM timeline_events WHERE
                           metadata::jsonb->'technical_evidence_revoked' = 'true'::jsonb
                       UNION ALL SELECT 1 FROM conversation_messages WHERE
                           metadata::jsonb->'context_evidence_withdrawn' = 'true'::jsonb
                       UNION ALL SELECT 1 FROM artifact_receipts WHERE
                           metadata::jsonb->'context_evidence_withdrawn' = 'true'::jsonb
                       UNION ALL SELECT 1 FROM actor_bindings WHERE
                           coalesce(metadata::jsonb->'context_generations', '{}'::jsonb) <> '{}'::jsonb)
    """)):
        raise RuntimeError("CONTEXT_INTEGRITY_DOWNGRADE_REQUIRES_NO_PILOT_HISTORY")
    for trigger, table in (
        ("context_artifact_revision", "artifacts"),
        ("context_candidate_technical_status", "candidate_insights"),
        ("context_episode_technical_status", "semantic_episodes"),
        ("context_message_tombstone", "conversation_messages"), ("context_event_tombstone", "timeline_events"),
        ("context_receipt_tombstone", "artifact_receipts"), ("context_journal_immutable", "audit_events"),
        ("context_membership_revision", "client_tenant_memberships"), ("context_binding_integrity", "actor_bindings"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for function in (
        "context_artifact_revision", "context_derivative_integrity", "context_tombstone_integrity", "context_journal_immutable", "context_membership_revision",
        "context_binding_integrity",
    ):
        op.execute(f"DROP FUNCTION {function}()")
    for table in REVISION_TABLES:
        op.drop_column(table, "context_revision")
