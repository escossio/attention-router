"""Only disposable test databases; never consumes a deployment database."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import Session

from attention_router.infrastructure.repository import seed_policies

pytestmark = pytest.mark.postgres


@pytest.fixture
def migration_db(pg_url):
    name = "direct_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(pg_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url)

    def migrate(direction, target, check=True):
        return subprocess.run(
            [sys.executable, "-m", "alembic", direction, target],
            env={**os.environ, "DATABASE_URL": url},
            check=check,
            capture_output=True,
            text=True,
        )

    try:
        migrate("upgrade", "0033_owner_automation_controls")
        with Session(engine) as s:
            seed_policies(s)
            s.commit()
        yield engine, migrate
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def legacy_control(engine, identifier="legacy", policy="desconhecido"):
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO owner_operational_controls
            (id,tenant_id,represented_owner_actor_key,control_key,policy_id,enabled,integer_value,
             revision,updated_by_actor_key,authorization_source,source_channel,source_event_id,
             provenance,created_at,updated_at)
            VALUES (:id,'00000000-0000-4000-8000-000000000001','owner','owner_reply_grace',:policy,
              true,10,3,'owner','OWNER_COMMAND','wwebjs','old-command','{"old":"preserved"}',now(),now())
        """),
            {"id": identifier, "policy": policy},
        )
        c.execute(
            text("""INSERT INTO owner_operational_control_changes
            (id,tenant_id,control_id,represented_owner_actor_key,control_key,policy_id,
             source_channel,source_event_id,requested_integer_value,previous_revision,
             resulting_revision,changed,provenance,created_at)
            VALUES (:id,'00000000-0000-4000-8000-000000000001',:id,'owner','owner_reply_grace',:policy,
              'wwebjs',:id,10,2,3,true,'{}',now())
        """),
            {"id": identifier, "policy": policy},
        )


def test_migration_preserves_ten_revision_three_and_roundtrip(migration_db):
    engine, migrate = migration_db
    legacy_control(engine)
    for _ in range(2):
        migrate("upgrade", "head")
        with engine.connect() as c:
            row = c.execute(text("SELECT * FROM owner_operational_controls")).mappings().one()
            assert (
                row["scope_type"],
                row["policy_id"],
                row["integer_value"],
                row["revision"],
                row["enabled"],
            ) == ("GLOBAL", None, 10, 3, True)
            assert row["id"] == "legacy" and row["provenance"]["old"] == "preserved"
            assert row["provenance"]["global_grace_migration"]["legacy_policy_id"] == "desconhecido"
            assert (
                c.execute(text("select count(*) from owner_operational_control_changes")).scalar()
                == 1
            )
            assert c.execute(
                text("select policy_id,resulting_revision from owner_operational_control_changes")
            ).one() == ("desconhecido", 3)
            assert next(
                x
                for x in inspect(c).get_columns("conversation_response_grace_windows")
                if x["name"] == "actor_binding_id"
            )["nullable"]
        migrate("downgrade", "0033_owner_automation_controls")
        with engine.connect() as c:
            assert c.execute(
                text("SELECT policy_id,integer_value,revision FROM owner_operational_controls")
            ).one() == ("desconhecido", 10, 3)
    migrate("upgrade", "head")


def test_ambiguous_legacy_controls_abort_without_partial_migration(migration_db):
    engine, migrate = migration_db
    legacy_control(engine)
    legacy_control(engine, "second", "mae")
    result = migrate("upgrade", "head", False)
    assert result.returncode != 0 and "GLOBAL_GRACE_MIGRATION_AMBIGUOUS" in result.stderr
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0033_owner_automation_controls"
        )
        assert not any(
            x["name"] == "scope_type" for x in inspect(c).get_columns("owner_operational_controls")
        )


def test_unknown_full_flow_on_postgres_without_binding(migration_db, monkeypatch):
    from tests.test_direct_conversation import (
        test_unknown_full_pipeline_without_binding as assert_flow,
    )

    engine, migrate = migration_db
    migrate("upgrade", "head")
    with Session(engine) as session:
        assert_flow(session, monkeypatch)


def test_migrated_history_replay_preserves_revision_and_provenance(migration_db):
    from attention_router.infrastructure.repository import upsert_actor_binding
    from attention_router.application.owner_operational_control import set_owner_reply_grace_seconds

    engine, migrate = migration_db
    legacy_control(engine)
    migrate("upgrade", "head")
    with Session(engine) as session:
        upsert_actor_binding(
            session,
            source="wwebjs",
            external_actor_id="owner-test@c.us",
            actor_key="owner",
            actor_category="owner",
            display_name="Owner",
            metadata={"owner": True},
        )
        common = dict(
            tenant_id="00000000-0000-4000-8000-000000000001",
            represented_owner_actor_key="owner",
            policy_id=None,
            updated_by_actor_key="owner",
            authorization_source="TEST",
            source_channel="wwebjs",
        )
        replay = set_owner_reply_grace_seconds(
            session, grace_seconds=10, source_event_id="legacy", **common
        )
        assert replay.duplicate and replay.control.control_revision == 3
        changed = set_owner_reply_grace_seconds(
            session, grace_seconds=15, source_event_id="new", **common
        )
        assert changed.control.control_revision == 4
        session.commit()
    with engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT provenance->'global_grace_migration'->>'legacy_policy_id' FROM owner_operational_controls"
                )
            ).scalar()
            == "desconhecido"
        )
    migrate("downgrade", "0034_global_direct_grace")
    result = migrate("downgrade", "0033_owner_automation_controls", False)
    assert result.returncode != 0
    assert "GLOBAL_GRACE_DOWNGRADE_REQUIRES_DATA_MIGRATION" in result.stderr
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "0034_global_direct_grace"
        )
        assert c.execute(
            text(
                "SELECT scope_type,policy_id,integer_value,revision FROM owner_operational_controls"
            )
        ).one() == ("GLOBAL", None, 15, 4)
        assert c.execute(
            text(
                "SELECT policy_id,resulting_revision FROM owner_operational_control_changes WHERE source_event_id='new'"
            )
        ).one() == (None, 4)
        assert c.execute(
            text(
                "SELECT policy_id,resulting_revision FROM owner_operational_control_changes WHERE source_event_id='legacy'"
            )
        ).one() == ("desconhecido", 3)
