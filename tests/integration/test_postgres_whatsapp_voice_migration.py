"""Disposable PostgreSQL checks for the WhatsApp voice schema boundary."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


@pytest.fixture
def migration_db(pg_url):
    name = "voice_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(
        pg_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
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
        migrate("upgrade", "0034_global_direct_grace")
        yield engine, migrate
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_voice_schema_upgrades_and_empty_roundtrip(migration_db):
    engine, migrate = migration_db
    migrate("upgrade", "head")
    with engine.connect() as connection:
        schema = inspect(connection)
        assert {
            "media_artifacts",
            "voice_transcriptions",
            "tts_derivations",
        }.issubset(schema.get_table_names())
        indexes = {item["name"] for item in schema.get_indexes("media_artifacts")}
        assert indexes == {
            "uq_media_artifact_inbound_voice",
            "uq_media_artifact_intent_tts",
        }
        checks = {item["name"] for item in schema.get_check_constraints("media_artifacts")}
        assert checks == {
            "ck_media_artifact_direction",
            "ck_media_artifact_purpose",
            "ck_media_artifact_status",
        }
    migrate("downgrade", "0034_global_direct_grace")
    with engine.connect() as connection:
        assert "media_artifacts" not in inspect(connection).get_table_names()
    migrate("upgrade", "head")


def test_voice_schema_refuses_downgrade_with_media(migration_db):
    engine, migrate = migration_db
    migrate("upgrade", "head")
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO media_artifacts
                (id, tenant_id, direction, purpose, content_sha256, media_kind,
                 mime_type, size_bytes, status, created_at, provenance)
                VALUES
                ('voice-artifact', '00000000-0000-4000-8000-000000000001',
                 'OUTBOUND', 'TTS_OUTPUT', :digest, 'ptt', 'audio/mpeg', 3,
                 'READY', now(), '{}')"""
            ),
            {"digest": "a" * 64},
        )
    result = migrate("downgrade", "0034_global_direct_grace", check=False)
    assert result.returncode != 0
    assert "WHATSAPP_VOICE_MEDIA_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            "0037_integration_admission_v0"
        )
        assert connection.execute(text("SELECT count(*) FROM media_artifacts")).scalar() == 1
