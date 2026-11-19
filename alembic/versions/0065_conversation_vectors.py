"""Passive conversation vector index; no cognitive/extraction integration."""

from alembic import op

revision = "0065_conversation_vectors"
down_revision = "0064_context_owner_scope"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE conversation_threads ADD CONSTRAINT uq_vector_thread_scope UNIQUE(id,tenant_id,source_account)"
    )
    op.execute(
        "ALTER TABLE conversation_messages ADD CONSTRAINT uq_vector_message_scope UNIQUE(id,tenant_id,source_account,conversation_id)"
    )
    op.execute("""CREATE TABLE conversation_vector_segments (
      id varchar(64) PRIMARY KEY, tenant_id varchar(64) NOT NULL,
      source_account varchar(180) NOT NULL, conversation_id varchar(64) NOT NULL,
      policy_version varchar(40) NOT NULL, content_text text NOT NULL,
      content_hash varchar(64) NOT NULL, source_hash varchar(64) NOT NULL,
      state varchar(20) NOT NULL CHECK(state IN ('PENDING','READY','STALE','FAILED')),
      generation bigint NOT NULL, start_at timestamptz NOT NULL, end_at timestamptz NOT NULL,
      attempts integer NOT NULL DEFAULT 0,error_code varchar(64),
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(id,tenant_id,source_account,conversation_id),UNIQUE(id,tenant_id,source_account),
      FOREIGN KEY(conversation_id,tenant_id,source_account) REFERENCES conversation_threads(id,tenant_id,source_account) ON DELETE CASCADE)""")
    op.execute("""CREATE TABLE conversation_vector_memberships (
      segment_id varchar(64) NOT NULL,tenant_id varchar(64) NOT NULL,
      source_account varchar(180) NOT NULL,conversation_id varchar(64) NOT NULL,
      message_id varchar(64) NOT NULL,ordinal integer NOT NULL,
      char_start integer NOT NULL,char_end integer NOT NULL,role varchar(20) NOT NULL,
      PRIMARY KEY(segment_id,ordinal),
      FOREIGN KEY(segment_id,tenant_id,source_account,conversation_id) REFERENCES conversation_vector_segments(id,tenant_id,source_account,conversation_id) ON DELETE CASCADE,
      FOREIGN KEY(message_id,tenant_id,source_account,conversation_id) REFERENCES conversation_messages(id,tenant_id,source_account,conversation_id) ON DELETE CASCADE,
      CHECK(char_start>=0 AND char_end>char_start))""")
    op.execute("""CREATE TABLE conversation_embeddings (
      segment_id varchar(64) NOT NULL,tenant_id varchar(64) NOT NULL,source_account varchar(180) NOT NULL,
      model varchar(160) NOT NULL,model_revision varchar(80) NOT NULL,dimensions integer NOT NULL CHECK(dimensions=384),
      content_hash varchar(64) NOT NULL,embedding vector(384) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(segment_id,model,model_revision),
      FOREIGN KEY(segment_id,tenant_id,source_account) REFERENCES conversation_vector_segments(id,tenant_id,source_account) ON DELETE CASCADE)""")
    op.execute(
        "CREATE INDEX ix_vector_scope ON conversation_vector_segments(tenant_id,source_account,conversation_id,state)"
    )
    op.execute(
        "CREATE INDEX ix_vector_membership_message ON conversation_vector_memberships(message_id)"
    )
    # Exact cosine scan is intentional at this corpus size; scope filters apply before ranking.
    op.execute("""CREATE TABLE conversation_vector_stats (
      name varchar(40) PRIMARY KEY,value double precision NOT NULL DEFAULT 0,updated_at timestamptz NOT NULL DEFAULT now())""")
    for table in [
        "conversation_vector_segments",
        "conversation_vector_memberships",
        "conversation_embeddings",
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY vector_scope ON {table} USING
          (tenant_id=current_setting('andy.vector_tenant',true) AND source_account=current_setting('andy.vector_account',true))
          WITH CHECK (tenant_id=current_setting('andy.vector_tenant',true) AND source_account=current_setting('andy.vector_account',true))""")
    op.execute("""CREATE FUNCTION enqueue_conversation_vector(cid varchar) RETURNS void LANGUAGE plpgsql AS $$
      DECLARE t record;
      BEGIN
        SELECT * INTO t FROM conversation_threads WHERE id=cid AND source='wwebjs';
        IF NOT FOUND THEN RETURN; END IF;
        -- Invalidations and source mutation commit atomically; stale vectors cannot be searched.
        DELETE FROM conversation_embeddings WHERE segment_id IN (SELECT id FROM conversation_vector_segments WHERE conversation_id=cid);
        UPDATE conversation_vector_segments SET state='STALE',content_text='',updated_at=now() WHERE conversation_id=cid;
        INSERT INTO queue(id,kind,status,payload,created_at) VALUES
          ('vector:'||cid,'conversation_vector_v1','PENDING',jsonb_build_object('conversation_id',cid,'tenant_id',t.tenant_id,'source_account',t.source_account,'generation',1,'next_at',0,'attempts',0),now())
        ON CONFLICT(id) DO UPDATE SET status='PENDING',processed_at=NULL,
          payload=EXCLUDED.payload||jsonb_build_object('generation',coalesce((queue.payload->>'generation')::bigint,0)+1);
      END $$""")
    op.execute("""CREATE FUNCTION conversation_vector_message_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='UPDATE' AND (NEW.text,NEW.searchable,NEW.sensitivity_class,NEW.message_type,NEW.tenant_id,NEW.source_account,NEW.conversation_id,NEW.sent_at,NEW.reply_to_message_id,NEW.from_me,NEW.direction,NEW.external_sender_key,NEW.sender_actor_id,NEW.metadata->>'source_inbound_event_id',NEW.metadata->>'deleted',NEW.metadata->>'revoked',NEW.metadata->>'content_state')
          IS NOT DISTINCT FROM (OLD.text,OLD.searchable,OLD.sensitivity_class,OLD.message_type,OLD.tenant_id,OLD.source_account,OLD.conversation_id,OLD.sent_at,OLD.reply_to_message_id,OLD.from_me,OLD.direction,OLD.external_sender_key,OLD.sender_actor_id,OLD.metadata->>'source_inbound_event_id',OLD.metadata->>'deleted',OLD.metadata->>'revoked',OLD.metadata->>'content_state') THEN RETURN NEW; END IF;
        IF TG_OP<>'INSERT' THEN PERFORM enqueue_conversation_vector(OLD.conversation_id); END IF;
        IF TG_OP<>'DELETE' AND (TG_OP='INSERT' OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id) THEN PERFORM enqueue_conversation_vector(NEW.conversation_id); END IF;
        RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
      END $$""")
    op.execute(
        "CREATE TRIGGER passive_vector_message AFTER INSERT OR UPDATE OR DELETE ON conversation_messages FOR EACH ROW EXECUTE FUNCTION conversation_vector_message_changed()"
    )
    op.execute("""CREATE FUNCTION conversation_vector_stt_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE cid varchar; eid varchar;
      BEGIN
        IF TG_OP='UPDATE' AND (NEW.status,NEW.transcript_text) IS NOT DISTINCT FROM (OLD.status,OLD.transcript_text) THEN RETURN NEW; END IF;
        eid=CASE WHEN TG_OP='DELETE' THEN OLD.inbound_event_id ELSE NEW.inbound_event_id END;
        FOR cid IN SELECT DISTINCT conversation_id FROM conversation_messages WHERE metadata->>'source_inbound_event_id'=eid LOOP
          PERFORM enqueue_conversation_vector(cid);
        END LOOP;
        RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
      END $$""")
    op.execute(
        "CREATE TRIGGER passive_vector_stt AFTER INSERT OR UPDATE OR DELETE ON voice_transcriptions FOR EACH ROW EXECUTE FUNCTION conversation_vector_stt_changed()"
    )
    op.execute("""CREATE FUNCTION conversation_vector_membership_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE cid varchar; tenant varchar;
      BEGIN
        tenant=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
        FOR cid IN SELECT id FROM conversation_threads WHERE tenant_id=tenant AND source='wwebjs' LOOP
          PERFORM enqueue_conversation_vector(cid);
        END LOOP;
        RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
      END $$""")
    op.execute(
        "CREATE TRIGGER passive_vector_owner AFTER INSERT OR UPDATE OR DELETE ON client_tenant_memberships FOR EACH ROW EXECUTE FUNCTION conversation_vector_membership_changed()"
    )
    op.execute(
        "SELECT enqueue_conversation_vector(id) FROM conversation_threads WHERE source='wwebjs'"
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    # Only derived, reproducible index data is removed. Originals/lexical/cognitive tables remain intact.
    op.execute("DROP TRIGGER passive_vector_owner ON client_tenant_memberships")
    op.execute("DROP TRIGGER passive_vector_stt ON voice_transcriptions")
    op.execute("DROP TRIGGER passive_vector_message ON conversation_messages")
    for fn in [
        "conversation_vector_membership_changed()",
        "conversation_vector_stt_changed()",
        "conversation_vector_message_changed()",
        "enqueue_conversation_vector(varchar)",
    ]:
        op.execute("DROP FUNCTION " + fn)
    op.execute("DELETE FROM queue WHERE kind='conversation_vector_v1'")
    for table in [
        "conversation_embeddings",
        "conversation_vector_memberships",
        "conversation_vector_segments",
        "conversation_vector_stats",
    ]:
        op.drop_table(table)
    op.drop_constraint("uq_vector_message_scope", "conversation_messages")
    op.drop_constraint("uq_vector_thread_scope", "conversation_threads")
    # Leave the reusable extension installed; no DROP EXTENSION CASCADE.
