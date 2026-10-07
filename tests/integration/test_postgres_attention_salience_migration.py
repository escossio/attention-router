"""Inspect Personal Context V2H attention/salience schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_attention_salience_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0057_bootstrap_selection"]

            schema = inspect(connection)
            assert "attention_assessments" in set(schema.get_table_names())

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "attention_assessments"
                )
            }
            assert uniques["uq_attention_assessment_snapshot"] == [
                "tenant_id",
                "signal_key",
                "snapshot_fingerprint",
            ]

            indexes = {
                item["name"]: item
                for item in schema.get_indexes("attention_assessments")
            }
            assert indexes[
                "uq_attention_assessment_active_signal"
            ]["unique"] is True

            checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "attention_assessments"
                )
            }
            assert {
                "ck_attention_assessment_source_type",
                "ck_attention_assessment_score",
                "ck_attention_assessment_score_class",
                "ck_attention_assessment_effective_class",
                "ck_attention_assessment_sensitivity",
                "ck_attention_assessment_status",
            } <= checks
    finally:
        engine.dispose()


@pytest.fixture
def attention_migration_db(pg_url):
    name = "attention_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(
        pg_url.rsplit("/", 1)[0] + "/postgres",
        isolation_level="AUTOCOMMIT",
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
        migrate("upgrade", "head")
        yield engine, migrate
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_attention_salience_refuses_destructive_downgrade(
    attention_migration_db,
):
    engine, migrate = attention_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000d056"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tenants
                    (id, slug, name, status, created_at, updated_at)
                VALUES
                    (:id, :slug, :name, 'ACTIVE', now(), now())
                """
            ),
            {
                "id": tenant_id,
                "slug": "attention-migration",
                "name": "Attention Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO attention_assessments (
                    id,
                    tenant_id,
                    signal_key,
                    snapshot_fingerprint,
                    source_type,
                    source_ref,
                    source_state,
                    score,
                    score_class,
                    effective_class,
                    components,
                    reason_codes,
                    sensitivity_class,
                    cooldown_until,
                    owner_suppressed_until,
                    owner_suppression_reason,
                    owner_suppressed_by,
                    acknowledged_at,
                    acknowledged_by,
                    owner_decision_ref,
                    status,
                    supersedes_assessment_id,
                    provenance,
                    created_at,
                    updated_at
                )
                VALUES (
                    'attention-migration',
                    :tenant_id,
                    'signal-1',
                    'fingerprint-1',
                    'OBLIGATION_INSTANCE',
                    'instance-1',
                    'UNCONFIRMED_AFTER_DUE',
                    0.9,
                    'OWNER_SUGGESTION_CANDIDATE',
                    'OWNER_SUGGESTION_CANDIDATE',
                    '{"impact": 1.0}'::jsonb,
                    '["HIGH_IMPACT"]'::jsonb,
                    'PRIVATE',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    'ACTIVE',
                    NULL,
                    '{"grants_authority": false}'::jsonb,
                    now(),
                    now()
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    result = migrate(
        "downgrade",
        "0055_obligation_expectation_v0",
        check=False,
    )
    assert result.returncode != 0
    assert "ATTENTION_SALIENCE_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0057_bootstrap_selection"
        assert connection.scalar(
            text("SELECT count(*) FROM attention_assessments")
        ) == 1
