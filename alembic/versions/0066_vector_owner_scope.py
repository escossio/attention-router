"""Invalidate both scopes if an owner membership is reassigned."""

from alembic import op

revision = "0066_vector_owner_scope"
down_revision = "0065_conversation_vectors"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("""CREATE OR REPLACE FUNCTION conversation_vector_membership_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE cid varchar;
      BEGIN
        IF TG_OP<>'INSERT' THEN
          FOR cid IN SELECT id FROM conversation_threads WHERE tenant_id=OLD.tenant_id AND source='wwebjs' LOOP
            PERFORM enqueue_conversation_vector(cid);
          END LOOP;
        END IF;
        IF TG_OP<>'DELETE' AND (TG_OP='INSERT' OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id) THEN
          FOR cid IN SELECT id FROM conversation_threads WHERE tenant_id=NEW.tenant_id AND source='wwebjs' LOOP
            PERFORM enqueue_conversation_vector(cid);
          END LOOP;
        END IF;
        RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
      END $$""")


def downgrade():
    op.execute("SET LOCAL lock_timeout='3s'")
    op.execute("""CREATE OR REPLACE FUNCTION conversation_vector_membership_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE cid varchar; tenant varchar;
      BEGIN
        tenant=CASE WHEN TG_OP='DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
        FOR cid IN SELECT id FROM conversation_threads WHERE tenant_id=tenant AND source='wwebjs' LOOP
          PERFORM enqueue_conversation_vector(cid);
        END LOOP;
        RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
      END $$""")
