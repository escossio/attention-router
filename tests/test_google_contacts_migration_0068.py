"""Regression tests for the additive Google Contacts migration.

Runs against a disposable in-memory database. CI can additionally repeat
this contract against a disposable PostgreSQL 16 instance.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sqlalchemy as sa
import pytest
from alembic.runtime.migration import MigrationContext
from alembic.operations import Operations


def migration(connection):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0068_google_contacts_cache.py"
    spec = spec_from_file_location("google_contacts_0068", path)
    instance = module_from_spec(spec)
    spec.loader.exec_module(instance)
    instance.op = Operations(MigrationContext.configure(connection))
    return instance


def test_google_contacts_rollback_preserves_existing_schema_data_and_index():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        m = migration(connection)
        m.upgrade()
        connection.execute(sa.insert(m.connections), {
            "id": "synthetic", "tenant_id": "test", "owner_id": "test",
            "status": "ACTIVE", "envelope": {}, "generation": "test",
            "next_run": 0.0, "failures": 0,
        })
        connection.execute(sa.insert(m.cache), {
            "connection_id": "synthetic", "resource": "people/synthetic",
            "person": {}, "search_name": "fixture", "phones": [],
            "emails": [], "generation": "test", "deleted": False,
        })
        m.upgrade()
        assert connection.execute(sa.select(sa.func.count()).select_from(m.cache)).scalar() == 1
        m.downgrade()
        assert connection.execute(sa.select(m.cache.c.resource)).scalar_one() == "people/synthetic"
        assert connection.execute(sa.select(m.connections.c.id)).scalar_one() == "synthetic"
        connection.execute(sa.delete(m.cache))
        connection.execute(sa.delete(m.connections))
        m.downgrade()
        assert set(sa.inspect(connection).get_table_names()) == {m.cache.name, m.connections.name}
        assert m.tenant_index.name in {
            idx["name"] for idx in sa.inspect(connection).get_indexes(m.connections.name)
        }
        m.upgrade()


def test_google_contacts_migration_rejects_namesake_schema_missing_columns():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE google_contacts_connections (id VARCHAR(64) NOT NULL PRIMARY KEY)"
        ))
        m = migration(connection)
        with pytest.raises(RuntimeError, match="SCHEMA_COLUMN_MISSING"):
            m.upgrade()
