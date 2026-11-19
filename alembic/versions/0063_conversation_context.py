"""Auditable conversation enrichment; no semantic execution."""
from alembic import op
import sqlalchemy as sa
revision = '0063_conversation_context'
down_revision = '0062_location_history'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE conversation_identity_evidence (
      event_id varchar(64) PRIMARY KEY REFERENCES inbound_events(id),
      tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
      source_account varchar(180) NOT NULL,
      lid varchar(240) NOT NULL, phone varchar(32) NOT NULL,
      observed_at timestamptz NOT NULL,
      provenance varchar(64) NOT NULL CHECK(provenance='PROVIDER_LID_PN_MAPPING'))''')
    op.execute('CREATE INDEX ix_context_evidence_scope ON conversation_identity_evidence(tenant_id,source_account,lid)')
    op.execute('''CREATE TABLE conversation_identity_links (
      conversation_id varchar(64) NOT NULL REFERENCES conversation_threads(id),
      participant_key varchar(240) NOT NULL,
      tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
      source_account varchar(180) NOT NULL,
      owner_id varchar(64) REFERENCES human_identities(id),
      state varchar(40) NOT NULL,
      evidence jsonb NOT NULL,
      revision integer NOT NULL DEFAULT 1,
      updated_at timestamptz NOT NULL,
      PRIMARY KEY(conversation_id,participant_key))''')
    op.execute('''CREATE TABLE conversation_identity_link_history (
      conversation_id varchar(64) NOT NULL, participant_key varchar(240) NOT NULL,
      revision integer NOT NULL, state varchar(40) NOT NULL, evidence jsonb NOT NULL,
      recorded_at timestamptz NOT NULL,
      PRIMARY KEY(conversation_id,participant_key,revision),
      FOREIGN KEY(conversation_id,participant_key) REFERENCES conversation_identity_links(conversation_id,participant_key))''')
    op.execute('''CREATE TABLE conversation_message_context (
      message_id varchar(64) PRIMARY KEY REFERENCES conversation_messages(id),
      tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
      event_id varchar(64) REFERENCES inbound_events(id),
      owner_id varchar(64) REFERENCES human_identities(id),
      location_state varchar(40) NOT NULL,
      device_id varchar(64), observation_id varchar(36), delta_seconds float,
      policy jsonb NOT NULL, reconciled_at timestamptz NOT NULL,
      FOREIGN KEY(tenant_id,device_id,observation_id)
        REFERENCES client_location_observations(tenant_id,device_id,observation_id),
      CHECK ((location_state='OWNER_DEVICE_TEMPORAL' AND device_id IS NOT NULL AND observation_id IS NOT NULL AND delta_seconds>=0)
         OR (location_state<>'OWNER_DEVICE_TEMPORAL' AND device_id IS NULL AND observation_id IS NULL AND delta_seconds IS NULL)))''')
    op.execute("""CREATE TABLE conversation_location_link_history (
      id bigserial PRIMARY KEY, message_id varchar(64) NOT NULL REFERENCES conversation_messages(id),
      previous_context jsonb, current_context jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE FUNCTION audit_conversation_location() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='INSERT' THEN
          INSERT INTO conversation_location_link_history(message_id,current_context) VALUES(NEW.message_id,to_jsonb(NEW)-'reconciled_at');
        ELSIF (to_jsonb(OLD)-'reconciled_at') IS DISTINCT FROM (to_jsonb(NEW)-'reconciled_at') THEN
          INSERT INTO conversation_location_link_history(message_id,previous_context,current_context)
          VALUES(NEW.message_id,to_jsonb(OLD)-'reconciled_at',to_jsonb(NEW)-'reconciled_at');
        END IF;
        RETURN NEW;
      END $$""")
    op.execute('CREATE TRIGGER context_location_audit AFTER INSERT OR UPDATE ON conversation_message_context FOR EACH ROW EXECUTE FUNCTION audit_conversation_location()')
    op.execute('CREATE INDEX ix_context_message_refresh ON conversation_message_context(reconciled_at)')
    op.execute('''CREATE TABLE conversation_context_progress (
      name varchar(32) PRIMARY KEY, cursor_time timestamptz, cursor_id varchar(64),
      cycles bigint NOT NULL DEFAULT 0, failures bigint NOT NULL DEFAULT 0,
      updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute("INSERT INTO conversation_context_progress(name) VALUES ('evidence'),('reconcile')")
    op.execute('CREATE INDEX ix_context_event_scan ON inbound_events(source,received_at,id)')
    op.execute('''CREATE VIEW conversation_context_v1 AS
      SELECT m.*, c.event_id, e.received_at AS original_received_at,
      e.processed_at AS original_processed_at,c.owner_id,
      coalesce(c.location_state,'LOCATION_UNKNOWN') AS owner_location_state,
      c.device_id AS owner_location_device_id,c.observation_id AS owner_location_observation_id,
      o.captured_at AS owner_location_captured_at,o.accuracy_m AS owner_location_accuracy_m,
      c.delta_seconds AS owner_location_delta_seconds,c.policy AS enrichment_policy,
      c.reconciled_at, s.state AS sender_identity_state,s.evidence AS sender_identity,
      peer.state AS interlocutor_identity_state,peer.evidence AS interlocutor_identity,
      (SELECT jsonb_agg(jsonb_build_object('artifact_id',a.id,'kind',a.media_kind,'status',a.status,'mime_type',a.mime_type))
       FROM media_artifacts a WHERE a.tenant_id=m.tenant_id AND a.inbound_event_id=c.event_id) AS media_references,
      (SELECT jsonb_agg(jsonb_build_object('transcription_id',v.id,'artifact_id',v.media_artifact_id,'status',v.status,'text',v.transcript_text,'completed_at',v.completed_at))
       FROM voice_transcriptions v WHERE v.tenant_id=m.tenant_id AND v.inbound_event_id=c.event_id) AS transcriptions
      FROM conversation_messages m
      JOIN conversation_threads t ON t.id=m.conversation_id AND t.tenant_id=m.tenant_id
      LEFT JOIN conversation_message_context c ON c.message_id=m.id AND c.tenant_id=m.tenant_id
      LEFT JOIN inbound_events e ON e.id=c.event_id AND e.tenant_id=m.tenant_id
      LEFT JOIN client_location_observations o ON o.tenant_id=c.tenant_id AND o.device_id=c.device_id AND o.observation_id=c.observation_id
      LEFT JOIN conversation_identity_links s ON s.conversation_id=m.conversation_id AND s.participant_key=m.external_sender_key AND s.tenant_id=m.tenant_id
      LEFT JOIN conversation_identity_links peer ON peer.conversation_id=m.conversation_id AND peer.participant_key=t.external_thread_key AND peer.tenant_id=m.tenant_id''')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM conversation_message_context LIMIT 1')).first():
        raise RuntimeError('CONTEXT_NONEMPTY_DOWNGRADE_REQUIRES_ARCHIVE')
    op.execute('DROP VIEW conversation_context_v1')
    op.execute('DROP TRIGGER context_location_audit ON conversation_message_context')
    op.execute('DROP FUNCTION audit_conversation_location()')
    for table in ['conversation_location_link_history','conversation_identity_link_history','conversation_identity_links','conversation_identity_evidence','conversation_message_context','conversation_context_progress']:
        op.drop_table(table)
    op.drop_index('ix_context_event_scan', table_name='inbound_events')
