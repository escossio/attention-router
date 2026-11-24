"""Staging-only proof of role DDL, RLS and owner/tenant/source isolation.

Role creation and ALTER TABLE run inside an explicit transaction that is
always rolled back. This test never writes credentials or touches production.
"""

from hashlib import sha256
from copy import deepcopy
from pathlib import Path
import runpy

import pytest
from sqlalchemy import text

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from test_postgres_client_context import context_reader  # noqa: F401
from test_postgres_cognitive_lab import lab  # noqa: F401
from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_postgres_source_consent import consent_clients  # noqa: F401

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("lab", ["client-query"], indirect=True)
def test_minimal_roles_restrict_tenant_account_owner_and_no_bypass(Session, consent_clients):  # noqa: F811
    _, writer, _, _, _ = consent_clients
    owner = writer._reader._scopes["owner"]
    role_module = runpy.run_path(str(
        Path(__file__).resolve().parents[2] / "ops/context-canary/role_plan.py"
    ))
    role_generator = role_module["generate"]
    rollback_generator = role_module["generate_rollback"]
    audit_catalog = role_module["audit_catalog"]
    tag = sha256(
        (owner.tenant_id + "\0" + owner.owner_id + "\0" + owner.source_account).encode()
    ).hexdigest()[:12]
    reader_role = f"andy_ctx_rd_{tag}"
    consent_role = f"andy_ctx_wr_{tag}"

    # The only table changes are inside a transaction that is always
    # rolled back, including CREATE ROLE, RLS and its policy attachments.
    with Session.kw["bind"].connect() as conn:
        tx = conn.begin()
        try:
            # Simulate pre-existing non-superuser integrations, including a
            # reader of a table that did NOT have RLS and an index reader
            # already restricted by the original pgvector GUC policy.
            conn.exec_driver_sql(
                "CREATE ROLE legacy_ctx_monitor NOLOGIN NOSUPERUSER NOBYPASSRLS"
            )
            conn.exec_driver_sql(
                "CREATE ROLE legacy_ctx_vector NOLOGIN NOSUPERUSER NOBYPASSRLS"
            )
            # Simulate a previously RLS-free source table and its original
            # monitoring reader. All DDL here is rolled back after this test.
            conn.exec_driver_sql(
                "ALTER TABLE public.conversation_messages DISABLE ROW LEVEL SECURITY"
            )
            conn.exec_driver_sql(
                "GRANT SELECT ON public.conversation_messages TO legacy_ctx_monitor"
            )
            conn.exec_driver_sql(
                "GRANT SELECT ON public.conversation_vector_segments TO legacy_ctx_vector"
            )
            original_vector_policy = conn.execute(text(
                "SELECT polname, pg_get_expr(polqual, polrelid),"
                "pg_get_expr(polwithcheck,polrelid) FROM pg_policy "
                "WHERE polrelid='public.conversation_vector_segments'::regclass "
                "ORDER BY polname"
            )).all()
            assert original_vector_policy
            legacy = audit_catalog(
                conn, legacy_roles=("legacy_ctx_monitor", "legacy_ctx_vector"),
            )
            assert legacy["tables"]["conversation_messages"]["roles"]["legacy_ctx_monitor"]["SELECT"]
            assert legacy["tables"]["conversation_vector_segments"]["rls"]
            plan = role_generator(
                tenant=owner.tenant_id, owner=owner.owner_id,
                account=owner.source_account, legacy_manifest=legacy,
            )
            # A stale catalog snapshot cannot authorize an RLS plan.
            altered = deepcopy(legacy)
            altered["tables"]["conversation_messages"]["roles"]["legacy_ctx_monitor"]["SELECT"] = False
            bad = role_generator(
                tenant=owner.tenant_id, owner=owner.owner_id,
                account=owner.source_account, legacy_manifest=altered,
            )
            first_check = next(
                line for line in bad.splitlines()
                if line.lstrip().startswith("DO $$")
                and "conversation_messages" in line
            )
            savepoint = conn.begin_nested()
            with pytest.raises(Exception, match="CANARY_LEGACY_CATALOG_DRIFT"):
                conn.exec_driver_sql(first_check)
            savepoint.rollback()

            # Exactly the previous grants, now supplemented by compatible
            # policies only where RLS used to be disabled.
            for line in plan.splitlines():
                if not line or line.startswith("--") or line in ("BEGIN;", "COMMIT;"):
                    continue
                conn.exec_driver_sql(line)

            # No RLS rewrite or permissive legacy policy is permitted on a
            # table where RLS was already protecting the vector GUC scope.
            current_vector_policy = conn.execute(text(
                "SELECT polname, pg_get_expr(polqual, polrelid),"
                "pg_get_expr(polwithcheck,polrelid) FROM pg_policy "
                "WHERE polrelid='public.conversation_vector_segments'::regclass "
                "ORDER BY polname"
            )).all()
            # Existing GUC-based vector policies are immutable. The only
            # additions are restricted to the two new canary principals.
            before = {row[0]: row[1:] for row in original_vector_policy}
            after = {row[0]: row[1:] for row in current_vector_policy}
            assert all(after.get(name) == values for name, values in before.items())
            assert all(name.startswith("ctx_" + tag + "_") for name in (after.keys() - before.keys()))

            # Legacy monitoring still sees its previous source access.
            conn.exec_driver_sql("SET ROLE legacy_ctx_monitor")
            assert conn.execute(text(
                "SELECT has_table_privilege(current_user,'public.conversation_messages','SELECT')"
            )).scalar_one() is True
            assert conn.execute(text(
                "SELECT count(*) FROM public.conversation_messages"
            )).scalar_one() > 0
            conn.exec_driver_sql("RESET ROLE")

            # The original vector GUC RLS policy is neither removed nor
            # overridden by a new permissive policy.
            conn.exec_driver_sql("SET ROLE legacy_ctx_vector")
            conn.execute(text(
                "SELECT pg_catalog.set_config('andy.vector_tenant',:tenant,true)"
            ), {"tenant": owner.tenant_id})
            conn.execute(text(
                "SELECT pg_catalog.set_config('andy.vector_account',:account,true)"
            ), {"account": owner.source_account})
            conn.execute(text(
                "SELECT count(*) FROM public.conversation_vector_segments"
            )).scalar_one()
            conn.exec_driver_sql("RESET ROLE")

            for role in (reader_role, consent_role):
                flags = conn.execute(text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin "
                    "FROM pg_roles WHERE rolname=:name"
                ), {"name": role}).one()
                assert flags == (False, False, False, False, True)
                assert conn.execute(text(
                    "SELECT rolpassword IS NULL FROM pg_authid WHERE rolname=:name"
                ), {"name": role}).scalar_one() is True

            conn.exec_driver_sql(f'SET ROLE "{reader_role}"')
            current = conn.execute(text("SELECT current_user")).scalar_one()
            assert current == reader_role
            assert conn.execute(text(
                "SELECT count(*) FROM conversation_messages "
                "WHERE tenant_id=:tenant AND source_account=:account"
            ), {"tenant": DEFAULT_TENANT_ID, "account": owner.source_account}).scalar_one() > 0
            assert conn.execute(text(
                "SELECT count(*) FROM conversation_messages WHERE tenant_id <> :tenant"
            ), {"tenant": DEFAULT_TENANT_ID}).scalar_one() == 0
            assert conn.execute(text(
                "SELECT count(*) FROM conversation_messages WHERE source_account <> :account"
            ), {"account": owner.source_account}).scalar_one() == 0

            # A SELECT-only actor cannot modify source text or the fence
            # enrollment table even if it knows their names and IDs.
            assert conn.execute(text(
                "SELECT has_table_privilege(current_user,"
                "'public.andy_context_fenced_tenants','SELECT')"
            )).scalar_one() is False
            assert conn.execute(text(
                "SELECT has_column_privilege(current_user,"
                "'public.conversation_messages','text','UPDATE')"
            )).scalar_one() is False
            assert conn.execute(text(
                "SELECT has_function_privilege(current_user,"
                "'public.andy_context_index_lock(text,boolean)','EXECUTE')"
            )).scalar_one() is True
            conn.exec_driver_sql("RESET ROLE")

            conn.exec_driver_sql(f'SET ROLE "{consent_role}"')
            assert conn.execute(text(
                "SELECT has_table_privilege(current_user,"
                "'public.context_source_consents','INSERT,UPDATE')"
            )).scalar_one() is True
            assert conn.execute(text(
                "SELECT has_table_privilege(current_user,"
                "'public.conversation_messages','INSERT')"
            )).scalar_one() is False
            conn.exec_driver_sql("RESET ROLE")

            # A rollback is permitted only once the protected tenant has
            # been explicitly unenrolled. A failure leaves all new policies
            # and roles intact because we use a savepoint.
            rollback = rollback_generator(
                tenant=owner.tenant_id, owner=owner.owner_id,
                account=owner.source_account, legacy_manifest=legacy,
            )
            first_guard = next(
                line for line in rollback.splitlines()
                if line.startswith("DO $$")
            )
            nested = conn.begin_nested()
            with pytest.raises(Exception, match="CANARY_ROLLBACK_DRAIN_REQUIRED"):
                conn.exec_driver_sql(first_guard)
            nested.rollback()
            conn.execute(text(
                "UPDATE public.andy_context_fenced_tenants SET enabled=false "
                "WHERE enabled=true"
            ))

            for line in rollback.splitlines():
                if not line or line.startswith("--") or line in ("BEGIN;", "COMMIT;"):
                    continue
                conn.exec_driver_sql(line)

            assert audit_catalog(
                conn, legacy_roles=("legacy_ctx_monitor", "legacy_ctx_vector"),
            ) == legacy
            for role in (reader_role, consent_role):
                assert conn.execute(text(
                    "SELECT count(*) FROM pg_roles WHERE rolname=:role"
                ), {"role": role}).scalar_one() == 0
            assert conn.execute(text(
                "SELECT polname, pg_get_expr(polqual, polrelid),"
                "pg_get_expr(polwithcheck,polrelid) FROM pg_policy "
                "WHERE polrelid='public.conversation_vector_segments'::regclass "
                "ORDER BY polname"
            )).all() == original_vector_policy
        finally:
            tx.rollback()
