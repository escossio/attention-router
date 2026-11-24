"""Transaction-held fence for the laboratory staged conversation index."""

from sqlalchemy import text

# Keep this list aligned with revision 0072. Queue protects generation changes
# even when enqueue_conversation_vector has no existing index rows to invalidate.
FENCED_TABLES = (
    "conversation_vector_segments", "conversation_vector_memberships", "conversation_embeddings",
    "conversation_messages", "conversation_threads", "client_tenant_memberships",
    "artifact_receipts", "artifacts", "inbound_events", "resources", "voice_transcriptions", "queue",
)
FENCE_SCHEMA = "0072_context_index_fence"

_COVERAGE = text("""
    SELECT count(*) FROM pg_catalog.pg_trigger t
    JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
      AND t.tgenabled = 'A' AND t.tgqual IS NULL AND t.tgattr = ''::int2vector
      AND ((t.tgname = 'andy_context_index_row' AND t.tgtype = 31
            AND t.tgfoid = to_regprocedure('public.andy_context_index_row()'))
        OR (t.tgname = 'andy_context_index_truncate' AND t.tgtype = 34
            AND t.tgfoid = to_regprocedure('public.andy_context_index_truncate()')))
""")


def _require_transaction(session):
    if (
        not session.in_transaction() or session.in_nested_transaction()
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise ValueError("CONTEXT_INDEX_FENCE_TRANSACTION_REQUIRED")
    connection = session.connection()
    if (
        connection.get_isolation_level() != "READ COMMITTED"
        or getattr(connection.connection.driver_connection, "autocommit", True)
    ):
        raise ValueError("CONTEXT_INDEX_FENCE_TRANSACTION_REQUIRED")


def fence_index(context):
    """Acquire before reading the candidate pool and keep through ASGI send."""
    session = context.session
    _require_transaction(session)
    if session.scalar(_COVERAGE, {"tables": list(FENCED_TABLES)}) != 2 * len(FENCED_TABLES):
        raise ValueError("CONTEXT_INDEX_FENCE_INCOMPLETE")
    session.execute(text("SELECT public.andy_context_index_lock(:tenant, true)"), {
        "tenant": context.arguments["tenant_id"],
    })


def fence_index_writer_if_installed(session, tenant):
    """Deployable workers still support schemas that predate the lab migration."""
    if session.scalar(text(
        "SELECT pg_catalog.to_regprocedure('public.andy_context_index_lock(text,boolean)') IS NOT NULL"
    )):
        _require_transaction(session)
        session.execute(text("SELECT public.andy_context_index_writer_lock(:tenant)"), {"tenant": tenant})
