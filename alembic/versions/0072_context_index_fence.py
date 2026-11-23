"""Tenant-enrolled context index fence, opt-in and fail-closed.

Every trigger is installed, but unaffected tenants do not acquire a fence.
No tenant is enrolled by migration. Enrollment requires a separate,
operator-approved operation after pausing/draining the relevant writers.
"""

from alembic import op

from attention_router.infrastructure.vector_attachment_provenance import CREATE_VECTOR_ATTACHMENT_PROVENANCE
from attention_router.config import settings
from attention_router.application.context_schema_release import context_schema_release_authorized

revision = "0072_context_index_fence"
down_revision = "0071_context_source_consent"
branch_labels = None
depends_on = None


# Freeze this revision independently of future reader changes. CI compares
# trigger coverage with the versioned reader protocol.
FENCED_TABLES = (
    "conversation_vector_segments", "conversation_vector_memberships", "conversation_embeddings",
    "conversation_messages", "conversation_threads", "client_tenant_memberships",
    "artifact_receipts", "artifacts", "inbound_events", "resources", "voice_transcriptions", "queue",
)


def require_release():
    if settings.app_env != "test" and not (
        settings.app_env == "production" and context_schema_release_authorized(op.get_bind())
    ):
        raise RuntimeError("CONTEXT_INDEX_FENCE_PROMOTION_NOT_AUTHORIZED")


def upgrade():
    require_release()
    op.execute(CREATE_VECTOR_ATTACHMENT_PROVENANCE)
    op.execute("REVOKE ALL ON public.andy_vector_attachment_provenance FROM PUBLIC")
    op.execute("""
        CREATE TABLE public.andy_context_fenced_tenants (
            tenant_id text PRIMARY KEY,
            enabled boolean NOT NULL DEFAULT false,
            enrolled_at timestamptz NOT NULL DEFAULT now()
        );
        REVOKE ALL ON public.andy_context_fenced_tenants FROM PUBLIC;
    """)
    op.execute("""
        CREATE FUNCTION public.andy_context_index_enrolled(tenant text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.andy_context_fenced_tenants
                WHERE tenant_id = tenant AND enabled IS TRUE
            )
        $$;
        REVOKE ALL ON FUNCTION public.andy_context_index_enrolled(text) FROM PUBLIC;
    """)
    op.execute("""
        CREATE FUNCTION public.andy_context_index_lock(tenant text, shared boolean) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE tenant_key integer;
        DECLARE acquired boolean;
        BEGIN
            IF tenant IS NULL OR tenant = '' THEN
                RAISE EXCEPTION 'CONTEXT_INDEX_FENCE_TENANT_REQUIRED';
            END IF;
            IF NOT public.andy_context_index_enrolled(tenant) THEN
                IF shared THEN
                    RAISE EXCEPTION 'CONTEXT_INDEX_FENCE_TENANT_NOT_ENROLLED'
                        USING ERRCODE = '42501';
                END IF;
                -- Existing writers outside the enrolled tenant proceed
                -- unchanged, even with an active canary reader elsewhere.
                RETURN;
            END IF;
            tenant_key := ('x' || substr(md5(convert_to(tenant, 'UTF8')), 1, 8))::bit(32)::integer;
            IF shared THEN
                acquired := pg_try_advisory_xact_lock_shared(1095648345, tenant_key);
            ELSE
                acquired := pg_try_advisory_xact_lock(1095648345, tenant_key);
            END IF;
            IF NOT acquired THEN
                RAISE EXCEPTION 'CONTEXT_INDEX_FENCE_BUSY' USING ERRCODE = '55P03';
            END IF;
        END $$;
        REVOKE ALL ON FUNCTION public.andy_context_index_lock(text,boolean) FROM PUBLIC;
    """)
    op.execute("""
        CREATE FUNCTION public.andy_context_index_writer_lock(tenant text) RETURNS void
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT public.andy_context_index_lock(tenant, false)
        $$;
        REVOKE ALL ON FUNCTION public.andy_context_index_writer_lock(text) FROM PUBLIC;
    """)
    op.execute("""
        CREATE FUNCTION public.andy_context_index_row() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE old_tenant text;
        DECLARE new_tenant text;
        BEGIN
            IF TG_TABLE_NAME = 'queue' THEN
                IF TG_OP = 'UPDATE' AND
                    (OLD.id, OLD.kind, OLD.payload->>'tenant_id', OLD.payload->>'source_account',
                     OLD.payload->>'conversation_id', OLD.payload->>'generation')
                    IS NOT DISTINCT FROM
                    (NEW.id, NEW.kind, NEW.payload->>'tenant_id', NEW.payload->>'source_account',
                     NEW.payload->>'conversation_id', NEW.payload->>'generation')
                THEN RETURN NEW; END IF;
                IF TG_OP <> 'INSERT' AND OLD.kind = 'conversation_vector_v1' THEN
                    PERFORM public.andy_context_index_lock(OLD.payload->>'tenant_id', false);
                END IF;
                IF TG_OP <> 'DELETE' AND NEW.kind = 'conversation_vector_v1' THEN
                    PERFORM public.andy_context_index_lock(NEW.payload->>'tenant_id', false);
                END IF;
            ELSE
                IF TG_OP <> 'INSERT' THEN old_tenant := OLD.tenant_id; END IF;
                IF TG_OP <> 'DELETE' THEN new_tenant := NEW.tenant_id; END IF;
                IF TG_OP <> 'INSERT' THEN
                    PERFORM public.andy_context_index_lock(old_tenant, false);
                END IF;
                IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND new_tenant IS DISTINCT FROM old_tenant) THEN
                    PERFORM public.andy_context_index_lock(new_tenant, false);
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$;
        REVOKE ALL ON FUNCTION public.andy_context_index_row() FROM PUBLIC;
    """)
    op.execute("""
        CREATE FUNCTION public.andy_context_index_truncate() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            -- A global TRUNCATE would remove protected tenant rows too.
            -- With no enrolled tenant, retain legacy operations unchanged.
            IF EXISTS (
                SELECT 1 FROM public.andy_context_fenced_tenants WHERE enabled IS TRUE
            ) THEN
                RAISE EXCEPTION 'CONTEXT_INDEX_FENCE_TRUNCATE_DENIED' USING ERRCODE = '0A000';
            END IF;
            RETURN NULL;
        END $$;
        REVOKE ALL ON FUNCTION public.andy_context_index_truncate() FROM PUBLIC;
    """)
    for table in FENCED_TABLES:
        op.execute(f"""
            CREATE TRIGGER andy_context_index_row
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.andy_context_index_row();
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER andy_context_index_row;
            CREATE TRIGGER andy_context_index_truncate
            BEFORE TRUNCATE ON public.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION public.andy_context_index_truncate();
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER andy_context_index_truncate;
        """)


def downgrade():
    require_release()
    op.execute("LOCK TABLE public.andy_context_fenced_tenants IN ACCESS EXCLUSIVE MODE")
    # Cannot silently detach the fence with a tenant still enrolled.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.andy_context_fenced_tenants) THEN
                RAISE EXCEPTION 'CONTEXT_INDEX_FENCE_DOWNGRADE_REQUIRES_DRAINED_TENANTS';
            END IF;
        END $$;
    """)
    for table in FENCED_TABLES:
        op.execute(f"DROP TRIGGER andy_context_index_truncate ON public.{table}")
        op.execute(f"DROP TRIGGER andy_context_index_row ON public.{table}")
    op.execute("DROP FUNCTION public.andy_context_index_truncate()")
    op.execute("DROP FUNCTION public.andy_context_index_row()")
    op.execute("DROP VIEW public.andy_vector_attachment_provenance")
    op.execute("DROP FUNCTION public.andy_context_index_writer_lock(text)")
    op.execute("DROP FUNCTION public.andy_context_index_lock(text, boolean)")
    op.execute("DROP FUNCTION public.andy_context_index_enrolled(text)")
    op.execute("DROP TABLE public.andy_context_fenced_tenants")
