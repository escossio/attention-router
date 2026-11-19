"""Keep source writers ahead of background index lock contention."""
from alembic import op
revision = "0067_vector_lock_order"
down_revision = "0066_vector_owner_scope"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("CREATE OR REPLACE FUNCTION enqueue_conversation_vector(cid varchar) RETURNS void LANGUAGE plpgsql AS $$\n      DECLARE t record;\n      BEGIN\n        SELECT * INTO t FROM conversation_threads WHERE id=cid AND source='wwebjs';\n        IF NOT FOUND THEN RETURN; END IF;\n        INSERT INTO queue(id,kind,status,payload,created_at) VALUES\n          ('vector:'||cid,'conversation_vector_v1','PENDING',jsonb_build_object('conversation_id',cid,'tenant_id',t.tenant_id,'source_account',t.source_account,'generation',1,'next_at',0,'attempts',0),now())\n        ON CONFLICT(id) DO UPDATE SET status='PENDING',processed_at=NULL,\n          payload=EXCLUDED.payload||jsonb_build_object('generation',coalesce((queue.payload->>'generation')::bigint,0)+1);\n        -- Invalidations and source mutation commit atomically; stale vectors cannot be searched.\n        DELETE FROM conversation_embeddings WHERE segment_id IN (SELECT id FROM conversation_vector_segments WHERE conversation_id=cid);\n        UPDATE conversation_vector_segments SET state='STALE',content_text='',updated_at=now() WHERE conversation_id=cid;\n      END $$")


def downgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("CREATE OR REPLACE FUNCTION enqueue_conversation_vector(cid varchar) RETURNS void LANGUAGE plpgsql AS $$\n      DECLARE t record;\n      BEGIN\n        SELECT * INTO t FROM conversation_threads WHERE id=cid AND source='wwebjs';\n        IF NOT FOUND THEN RETURN; END IF;\n        -- Invalidations and source mutation commit atomically; stale vectors cannot be searched.\n        DELETE FROM conversation_embeddings WHERE segment_id IN (SELECT id FROM conversation_vector_segments WHERE conversation_id=cid);\n        UPDATE conversation_vector_segments SET state='STALE',content_text='',updated_at=now() WHERE conversation_id=cid;\n        INSERT INTO queue(id,kind,status,payload,created_at) VALUES\n          ('vector:'||cid,'conversation_vector_v1','PENDING',jsonb_build_object('conversation_id',cid,'tenant_id',t.tenant_id,'source_account',t.source_account,'generation',1,'next_at',0,'attempts',0),now())\n        ON CONFLICT(id) DO UPDATE SET status='PENDING',processed_at=NULL,\n          payload=EXCLUDED.payload||jsonb_build_object('generation',coalesce((queue.payload->>'generation')::bigint,0)+1);\n      END $$")
