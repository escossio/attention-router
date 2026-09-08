"""PostgreSQL migration contract for revision 0026."""

import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_10_5c_6i1a_r2_bridge_adversarial import (
    _decision, _legacy_scenario_version, _production_agent, _run,
)

pytestmark = pytest.mark.postgres


def _admin_url() -> str:
    return os.environ["DATABASE_URL"]


def _db_url(name: str) -> str:
    return _admin_url().rsplit("/", 1)[0] + f"/{name}"


def _migrate(url: str, revision: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    subprocess.run(
        ["python", "-m", "alembic", "upgrade", revision],
        cwd=Path(__file__).resolve().parents[2], env=env, check=True,
        capture_output=True, text=True,
    )


def _downgrade(url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    subprocess.run(["python", "-m", "alembic", "downgrade", "0025_human_auth_execution_intent"], cwd=Path(__file__).resolve().parents[2], env=env, check=True, capture_output=True, text=True)


def _seed(url: str) -> sessionmaker:
    session = sessionmaker(bind=create_engine(url, future=True), expire_on_commit=False, future=True)
    return session


def _head(url: str) -> sessionmaker:
    _migrate(url, "0026_production_authority_scope")
    return _seed(url)


@pytest.fixture()
def fresh_db():
    name = f"migration_0026_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _db_url(name)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_m01_zero_row_upgrade_0025_to_0026(fresh_db):
    _migrate(fresh_db, "0025_human_auth_execution_intent")
    engine = create_engine(fresh_db, future=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0025_human_auth_execution_intent"
        assert conn.execute(text("SELECT count(*) FROM execution_intents")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM agent_execution_intents")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM scenario_runs")).scalar_one() == 0
    engine.dispose()
    _migrate(fresh_db, "0026_production_authority_scope")
    engine = create_engine(fresh_db, future=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0026_production_authority_scope"
        columns = {row[0] for row in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='agent_execution_intents'"))}
        assert {"execution_intent_id", "execution_intent_fingerprint"} <= columns
        run_columns = {row[0] for row in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='scenario_runs'"))}
        assert "agent_execution_intent_id" in run_columns
        assert conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname='uq_agent_execution_intents_production_parent'")).scalar_one()
        assert conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname='uq_scenario_run_production_agent_intent'")).scalar_one()
        assert conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname='ck_agent_execution_intent_bridge_pair'")).scalar_one()
        fk_names = {row[0] for row in conn.execute(text("SELECT conname FROM pg_constraint WHERE contype='f' AND conrelid IN ('agent_execution_intents'::regclass, 'scenario_runs'::regclass)"))}
        assert 'agent_execution_intents_execution_intent_id_fkey' in fk_names
        assert 'scenario_runs_agent_execution_intent_id_fkey' in fk_names
        assert conn.execute(text("SELECT 1 FROM pg_trigger WHERE tgname='agent_execution_intents_bridge_immutable'")).scalar_one()
        assert conn.execute(text("SELECT 1 FROM pg_trigger WHERE tgname='scenario_runs_bridge_immutable'")).scalar_one()
    engine.dispose()


def test_m02_legacy_agent_survives_upgrade(fresh_db):
    _migrate(fresh_db, "0025_human_auth_execution_intent")
    Session = _seed(fresh_db)
    with Session() as db:
        decision = _decision(db)
        legacy_id = f"legacy-{uuid.uuid4().hex}"
        db.execute(text("""INSERT INTO agent_execution_intents
            (id, agent_decision_id, authorization_source, intent_type,
             effective_response_snapshot, status, execution_allowed,
             external_delivery_allowed, release_status, idempotency_key, created_at)
            VALUES (:id, :decision, 'LEGACY', 'legacy', 'ok', 'PENDING', false,
                    false, 'HELD', :key, :created)"""),
            {"id": legacy_id, "decision": decision, "key": uuid.uuid4().hex, "created": datetime.now(UTC)})
        db.commit()
    _migrate(fresh_db, "0026_production_authority_scope")
    with create_engine(fresh_db, future=True).connect() as db:
        row = db.execute(text("SELECT execution_intent_id, execution_intent_fingerprint FROM agent_execution_intents WHERE id=:id"), {"id": legacy_id}).one()
        assert row == (None, None)


def test_m03_legacy_run_survives_upgrade(fresh_db):
    _migrate(fresh_db, "0025_human_auth_execution_intent")
    Session = _seed(fresh_db)
    with Session() as db:
        suffix = uuid.uuid4().hex
        now = datetime.now(UTC)
        definition_id = f"r2-sd-{suffix}"
        version_id = f"r2-sv-{suffix}"
        db.execute(text("INSERT INTO scenario_definitions (id, tenant_id, scenario_key, title, description, created_at, updated_at) VALUES (:id, :tenant, :key, 'Legacy', 'fixture', :now, :now)"), {"id": definition_id, "tenant": "00000000-0000-4000-8000-000000000001", "key": f"legacy-{suffix}", "now": now})
        db.execute(text("""INSERT INTO scenario_versions
            (id, tenant_id, scenario_definition_id, version, schema_version,
             manifest_source_path, content_hash, requirements_covered, risk_ids,
             config_keys, risk_classification, enabled, source_sha, created_at,
             is_immutable)
            VALUES (:id, :tenant, :definition, 1, 'r2', 'tests/r2.yaml', :hash,
                    '[]', '[]', '[]', 'LOW', true, 'r2', :now, true)"""), {"id": version_id, "tenant": "00000000-0000-4000-8000-000000000001", "definition": definition_id, "hash": suffix, "now": now})
        run_id = f"r2-run-{suffix}"
        db.execute(text("""INSERT INTO scenario_runs
            (id, tenant_id, scenario_version_id, status, root_correlation_id,
             source_sha, schema_revision, created_at, expires_at, cleanup_state,
             updated_at)
            VALUES (:id, :tenant, :version, 'CREATED', :correlation, 'r2', 'r2',
                    :now, :expires, 'PENDING', :now)"""), {"id": run_id, "tenant": "00000000-0000-4000-8000-000000000001", "version": version_id, "correlation": f"r2-c-{suffix}", "now": now, "expires": now + timedelta(minutes=5)})
        db.commit()
    _migrate(fresh_db, "0026_production_authority_scope")
    with create_engine(fresh_db, future=True).connect() as db:
        assert db.execute(text("SELECT agent_execution_intent_id FROM scenario_runs WHERE id=:id"), {"id": run_id}).scalar_one() is None


def test_m04_legacy_upgrade_has_no_backfill(fresh_db):
    _migrate(fresh_db, "0025_human_auth_execution_intent")
    Session = _seed(fresh_db)
    with Session() as db:
        decision = _decision(db)
        legacy_id = f"legacy-{uuid.uuid4().hex}"
        db.execute(text("""INSERT INTO agent_execution_intents
            (id, agent_decision_id, authorization_source, intent_type,
             effective_response_snapshot, status, execution_allowed,
             external_delivery_allowed, release_status, idempotency_key, created_at)
            VALUES (:id, :decision, 'LEGACY', 'legacy', 'ok', 'PENDING', false,
                    false, 'HELD', :key, :created)"""),
            {"id": legacy_id, "decision": decision, "key": uuid.uuid4().hex, "created": datetime.now(UTC)})
        db.commit()
    _migrate(fresh_db, "0026_production_authority_scope")
    with create_engine(fresh_db, future=True).connect() as db:
        assert db.execute(text("SELECT count(*) FROM execution_intents")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM agent_execution_intents WHERE execution_intent_id IS NOT NULL")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM scenario_runs WHERE agent_execution_intent_id IS NOT NULL")).scalar_one() == 0


def test_m05_empty_downgrade(fresh_db):
    _head(fresh_db)
    _downgrade(fresh_db)
    with create_engine(fresh_db, future=True).connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0025_human_auth_execution_intent"


def test_m06_agent_bridge_blocks_downgrade(fresh_db):
    Session = _head(fresh_db)
    with Session() as db:
        parent, _ = _production_agent(db)
        db.commit()
    with pytest.raises(subprocess.CalledProcessError):
        _downgrade(fresh_db)


def test_m07_agent_bridge_preserved_after_block(fresh_db):
    Session = _head(fresh_db)
    with Session() as db:
        parent, agent_id = _production_agent(db)
        db.commit()
    with pytest.raises(subprocess.CalledProcessError):
        _downgrade(fresh_db)
    with create_engine(fresh_db, future=True).connect() as db:
        row = db.execute(text("SELECT execution_intent_id, execution_intent_fingerprint FROM agent_execution_intents WHERE id=:id"), {"id": agent_id}).one()
        assert row == (parent.id, parent.scope_fingerprint)


def test_m08_run_bridge_blocks_downgrade(fresh_db):
    Session = _head(fresh_db)
    with Session() as db:
        _, agent_id = _production_agent(db)
        _legacy_scenario_version(db)
        _run(db, agent_id)
        db.commit()
    with pytest.raises(subprocess.CalledProcessError):
        _downgrade(fresh_db)


def test_m09_run_bridge_preserved_after_block(fresh_db):
    Session = _head(fresh_db)
    with Session() as db:
        parent, agent_id = _production_agent(db)
        _legacy_scenario_version(db)
        run = _run(db, agent_id)
        db.commit()
    with pytest.raises(subprocess.CalledProcessError):
        _downgrade(fresh_db)
    with create_engine(fresh_db, future=True).connect() as db:
        agent_bridge = db.execute(text(
            "SELECT execution_intent_id, execution_intent_fingerprint "
            "FROM agent_execution_intents WHERE id=:id"
        ), {"id": agent_id}).one()
        assert agent_bridge == (parent.id, parent.scope_fingerprint)
        assert db.execute(text("SELECT agent_execution_intent_id FROM scenario_runs WHERE id=:id"), {"id": run.id}).scalar_one() == agent_id


def test_m10_materialized_history_blocks_downgrade(fresh_db):
    Session = _head(fresh_db)
    with Session() as db:
        parent, _ = _production_agent(db)
        db.commit()
    with pytest.raises(subprocess.CalledProcessError):
        _downgrade(fresh_db)


def _roundtrip(url: str) -> None:
    _migrate(url, "0025_human_auth_execution_intent")
    _migrate(url, "0026_production_authority_scope")
    _downgrade(url)
    _migrate(url, "0026_production_authority_scope")


def test_m11_roundtrip_fresh_database_one(fresh_db):
    _roundtrip(fresh_db)
    with create_engine(fresh_db, future=True).connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0026_production_authority_scope"


def test_m12_roundtrip_fresh_database_two(fresh_db):
    _roundtrip(fresh_db)
    with create_engine(fresh_db, future=True).connect() as db:
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0026_production_authority_scope"
