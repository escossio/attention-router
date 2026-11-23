"""Synthetic G3/G4 contracts. PostgreSQL DDL and temporary roles roll back together."""
from pathlib import Path
import runpy
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from attention_router.application.context_index_fence import FENCED_TABLES, fence_index
from attention_router.application.context_schema_release import approved_context_schema_release
from attention_router.config import settings

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
BASE = "0067_vector_lock_order"
HEAD = "0072_context_index_fence"


def migrate(conn, action, target):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = conn
    with approved_context_schema_release(conn):
        getattr(command, action)(cfg, target)


def test_schema_roundtrip_contacts_allowlist_and_consent(Session, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    with Session.kw["bind"].connect() as conn, conn.begin():
        conn.exec_driver_sql("SET LOCAL lock_timeout='1500ms'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='20000ms'")
        conn.exec_driver_sql("""
            INSERT INTO google_contacts_connections
            (id,tenant_id,owner_id,status,envelope,generation,next_run,failures)
            VALUES ('fixture-contact','fixture-tenant','fixture-owner','ACTIVE','{}','a',0,0)
        """)
        migrate(conn, "downgrade", BASE)
        assert conn.scalar(text("SELECT id FROM google_contacts_connections")) == "fixture-contact"
        migrate(conn, "upgrade", HEAD)
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert conn.scalar(text("SELECT COUNT(*) FROM public.andy_context_fenced_tenants")) == 0
        assert conn.scalar(text("SELECT COUNT(*) FROM public.context_source_consents")) == 0
        assert conn.scalar(text("SELECT COUNT(*) FROM pg_trigger WHERE tgname IN "
                                "('andy_context_index_row','andy_context_index_truncate') "
                                "AND tgenabled='A'")) == 2 * len(FENCED_TABLES)
        # Direct downgrade refuses production mode without the reviewed runner.
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="PROMOTION_NOT_AUTHORIZED"):
            command.downgrade(cfg, "0071_context_source_consent")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_enrollment_and_consent_history_block_downgrade(Session):
    with Session.kw["bind"].connect() as conn:
        tx = conn.begin()
        try:
            conn.exec_driver_sql("INSERT INTO andy_context_fenced_tenants(tenant_id,enabled) VALUES ('fixture',true)")
            with pytest.raises(DBAPIError, match="REQUIRES_DRAINED_TENANTS"), conn.begin_nested():
                migrate(conn, "downgrade", BASE)
            conn.exec_driver_sql("DELETE FROM andy_context_fenced_tenants")
            conn.exec_driver_sql("""
                INSERT INTO context_consent_scopes
                (id,tenant_id,owner_id,binding_id,source_account,resource_id,
                 scope_start,incarnation,purpose) VALUES
                ('s','t','o','b','a','r',now(),'v','test')
            """)
            with pytest.raises(DBAPIError, match="REQUIRES_EMPTY_AUTHORITY"), conn.begin_nested():
                migrate(conn, "downgrade", BASE)
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert conn.scalar(text("SELECT id FROM context_consent_scopes")) == "s"
        finally:
            tx.rollback()


def test_rls_install_legacy_writer_and_reversal(Session):
    plan = runpy.run_path(str(ROOT / "ops/context-canary/role_plan.py"))
    release = runpy.run_path(str(ROOT / "ops/context-canary/release_roles.py"))
    token = uuid4().hex[:12]
    indexer, monitor = f"fixture_vi_{token}", f"fixture_mon_{token}"
    args = dict(tenant="synthetic-tenant", owner="synthetic-owner", account="synthetic-account")
    reader, writer = release["role_names"](**args)
    with Session.kw["bind"].connect() as conn:
        tx = conn.begin()
        try:
            for role in (indexer, monitor):
                conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS")
            conn.exec_driver_sql(f"GRANT SELECT ON conversation_messages TO {indexer}")
            conn.exec_driver_sql(f"GRANT INSERT ON conversation_vector_segments, conversation_embeddings TO {indexer}")
            conn.exec_driver_sql(f"GRANT UPDATE(payload) ON queue TO {indexer}")
            conn.exec_driver_sql(f"GRANT SELECT(id), UPDATE(status) ON resources TO {monitor}")
            before = plan["audit_catalog"](conn, legacy_roles=[indexer, monitor])
            args.update(legacy_manifest=before, indexer_roles=[indexer])
            for line in plan["generate"](**args).splitlines():
                if line and not line.startswith("--") and line not in {"BEGIN;", "COMMIT;"}:
                    conn.exec_driver_sql(line)
            for role in (reader, writer):
                release["_verify_effective_privileges"](conn, role, reader=role == reader, plan=plan)
                assert conn.scalar(text("SELECT NOT rolsuper AND NOT rolbypassrls AND rolcanlogin "
                                        "FROM pg_roles WHERE rolname=:name"), {"name": role})
            assert conn.scalar(text("SELECT count(*) FROM public.context_consent_scopes")) == 0
            assert conn.scalar(text("SELECT count(*) FROM public.andy_context_fenced_tenants")) == 0
            conn.exec_driver_sql(f"SET LOCAL ROLE {indexer}")
            conn.exec_driver_sql("SELECT public.andy_context_index_writer_lock('unenrolled')")
            assert not conn.scalar(text("SELECT has_function_privilege(current_user,"
                                        "'public.andy_context_index_lock(text,boolean)','EXECUTE')"))
            assert not conn.scalar(text("SELECT has_table_privilege(current_user,'artifacts','SELECT')"))
            assert conn.scalar(text("SELECT count(*) FROM public.andy_vector_attachment_provenance")) == 0
            conn.exec_driver_sql("RESET ROLE")
            conn.exec_driver_sql(f"SET LOCAL ROLE {monitor}")
            # Column-level writer still has its corresponding RLS UPDATE policy.
            assert conn.scalar(text("SELECT count(*) FROM pg_policies WHERE schemaname='public' "
                                    "AND tablename='resources' AND cmd='UPDATE' "
                                    "AND current_user=ANY(roles)")) == 1
            conn.exec_driver_sql("UPDATE resources SET status='ACTIVE' WHERE id='absent'")
            conn.exec_driver_sql("RESET ROLE")
            conn.exec_driver_sql(f'SET LOCAL ROLE "{writer}"')
            with pytest.raises(DBAPIError), conn.begin_nested():
                conn.exec_driver_sql("""
                    INSERT INTO context_consent_scopes
                    (id,tenant_id,owner_id,binding_id,source_account,resource_id,scope_start,incarnation,purpose)
                    VALUES ('s','other-tenant','synthetic-owner','b','synthetic-account','r',now(),'v','test')
                """)
            conn.exec_driver_sql("RESET ROLE")
            for line in plan["generate_rollback"](**args).splitlines():
                if line and not line.startswith("--") and line not in {"BEGIN;", "COMMIT;"}:
                    conn.exec_driver_sql(line)
            assert plan["audit_catalog"](conn, legacy_roles=[indexer, monitor]) == before
            assert not conn.scalar(text("SELECT has_function_privilege(:role,"
                                        "'public.andy_context_index_writer_lock(text)','EXECUTE')"), {"role": indexer})
        finally:
            tx.rollback()


def test_enrolled_reader_allows_other_tenant_and_writer_recovers(Session):
    tenant = "synthetic-enrolled"
    with Session.begin() as db:
        db.execute(text("INSERT INTO andy_context_fenced_tenants(tenant_id,enabled) VALUES (:tenant,true)"),
                   {"tenant": tenant})
    try:
        with Session.begin() as reader:
            fence_index(SimpleNamespace(session=reader, arguments={"tenant_id": tenant}))
            with Session.begin() as writer:
                writer.execute(text("SELECT public.andy_context_index_writer_lock('other-tenant')"))
            with pytest.raises(DBAPIError) as refused, Session.begin() as writer:
                writer.execute(text("SELECT public.andy_context_index_writer_lock(:tenant)"), {"tenant": tenant})
            assert refused.value.orig.sqlstate == "55P03"
        with Session.begin() as writer:
            writer.execute(text("SELECT public.andy_context_index_writer_lock(:tenant)"), {"tenant": tenant})
    finally:
        with Session.begin() as db:
            db.execute(text("DELETE FROM andy_context_fenced_tenants WHERE tenant_id=:tenant"), {"tenant": tenant})
