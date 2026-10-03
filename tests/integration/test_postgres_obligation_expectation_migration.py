"""Inspect Personal Context V2G obligation/expectation schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_obligation_expectation_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0055_obligation_expectation_v0"]

            schema = inspect(connection)
            assert {
                "recurring_obligation_definitions",
                "obligation_instances",
                "obligation_fulfillments",
                "obligation_transitions",
            } <= set(schema.get_table_names())

            definition_indexes = {
                item["name"]: item
                for item in schema.get_indexes(
                    "recurring_obligation_definitions"
                )
            }
            assert definition_indexes[
                "uq_obligation_definition_active_semantic"
            ]["unique"] is True

            definition_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "recurring_obligation_definitions"
                )
            }
            assert definition_uniques[
                "uq_obligation_definition_tenant_idempotency"
            ] == ["tenant_id", "idempotency_key"]

            instance_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "obligation_instances"
                )
            }
            assert instance_uniques[
                "uq_obligation_instance_definition_period"
            ] == ["definition_id", "period_key"]

            fulfillment_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "obligation_fulfillments"
                )
            }
            assert fulfillment_uniques[
                "uq_obligation_fulfillment_instance_event"
            ] == ["instance_id", "timeline_event_id"]

            definition_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "recurring_obligation_definitions"
                )
            }
            assert {
                "ck_obligation_definition_subject_type",
                "ck_obligation_definition_cadence",
                "ck_obligation_definition_due_day",
                "ck_obligation_definition_grace",
                "ck_obligation_definition_confidence",
                "ck_obligation_definition_sensitivity",
                "ck_obligation_definition_source_kind",
                "ck_obligation_definition_state",
                "ck_obligation_definition_temporal_order",
            } <= definition_checks

            instance_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "obligation_instances"
                )
            }
            assert {
                "ck_obligation_instance_state",
                "ck_obligation_instance_reconciliation",
                "ck_obligation_instance_satisfaction",
                "ck_obligation_instance_sensitivity",
                "ck_obligation_instance_period_order",
                "ck_obligation_instance_due_window_order",
            } <= instance_checks
    finally:
        engine.dispose()


@pytest.fixture
def obligation_migration_db(pg_url):
    name = "obligation_migration_" + uuid.uuid4().hex[:12]
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


def test_obligation_expectation_refuses_destructive_downgrade(
    obligation_migration_db,
):
    engine, migrate = obligation_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000d055"

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
                "slug": "obligation-migration",
                "name": "Obligation Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recurring_obligation_definitions (
                    id,
                    tenant_id,
                    semantic_key,
                    idempotency_key,
                    subject_type,
                    subject_id,
                    obligation_kind,
                    expected_actor_key,
                    expected_event_type,
                    value_constraints,
                    cadence_kind,
                    due_day,
                    due_timezone,
                    grace_seconds,
                    confidence,
                    sensitivity_class,
                    source_kind,
                    source_ref,
                    provenance,
                    state,
                    supersedes_definition_id,
                    valid_from,
                    valid_until,
                    created_at,
                    updated_at
                )
                VALUES (
                    'definition-migration',
                    :tenant_id,
                    'obligation-semantic-key',
                    'obligation-idempotency-key',
                    'RESOURCE',
                    'resource-1',
                    'RENT',
                    'actor-1',
                    'RENT_PAYMENT',
                    '{"amount": 1350}'::jsonb,
                    'MONTHLY',
                    10,
                    'America/Fortaleza',
                    0,
                    1.0,
                    'PRIVATE',
                    'OWNER_DECLARED',
                    NULL,
                    '{"grants_authority": false}'::jsonb,
                    'ACTIVE',
                    NULL,
                    now(),
                    NULL,
                    now(),
                    now()
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    result = migrate(
        "downgrade",
        "0054_candidate_insight_v0",
        check=False,
    )
    assert result.returncode != 0
    assert "OBLIGATION_EXPECTATION_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0055_obligation_expectation_v0"
        assert connection.scalar(
            text("SELECT count(*) FROM recurring_obligation_definitions")
        ) == 1
