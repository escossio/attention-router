"""Inspect Personal Context V2C entity resolution schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_entity_resolution_migration_schema():
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
            tables = set(schema.get_table_names())
            assert {
                "entity_resolution_candidates",
                "entity_resolution_evidence",
                "entity_alias_resolutions",
            } <= tables

            candidate_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "entity_resolution_candidates"
                )
            }
            assert candidate_uniques[
                "uq_entity_resolution_candidate_idempotency"
            ] == ["idempotency_key"]
            assert candidate_uniques[
                "uq_entity_resolution_candidate_pair_evidence"
            ] == [
                "tenant_id",
                "left_actor_key",
                "right_actor_key",
                "evidence_fingerprint",
            ]

            evidence_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "entity_resolution_evidence"
                )
            }
            assert evidence_uniques[
                "uq_entity_resolution_evidence_idempotency"
            ] == ["idempotency_key"]

            alias_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "entity_alias_resolutions"
                )
            }
            assert alias_uniques[
                "uq_entity_alias_resolution_candidate"
            ] == ["candidate_id"]

            candidate_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "entity_resolution_candidates"
                )
            }
            assert {
                "ck_entity_resolution_candidate_pair_order",
                "ck_entity_resolution_candidate_confidence",
                "ck_entity_resolution_candidate_evidence_count",
                "ck_entity_resolution_candidate_state",
            } <= candidate_checks

            evidence_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "entity_resolution_evidence"
                )
            }
            assert {
                "ck_entity_resolution_evidence_type",
                "ck_entity_resolution_evidence_confidence",
            } <= evidence_checks

            alias_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "entity_alias_resolutions"
                )
            }
            assert {
                "ck_entity_alias_resolution_distinct_actor",
                "ck_entity_alias_resolution_state",
            } <= alias_checks

            alias_indexes = {
                item["name"]: item
                for item in schema.get_indexes("entity_alias_resolutions")
            }
            assert alias_indexes[
                "uq_entity_alias_resolution_active_alias"
            ]["unique"] is True
            assert (
                "ix_entity_alias_resolution_canonical"
                in alias_indexes
            )
    finally:
        engine.dispose()


@pytest.fixture
def entity_resolution_migration_db(pg_url):
    name = "entity_resolution_migration_" + uuid.uuid4().hex[:12]
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


def test_entity_resolution_refuses_destructive_downgrade(
    entity_resolution_migration_db,
):
    engine, migrate = entity_resolution_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000c052"

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
                "slug": "entity-resolution-migration",
                "name": "Entity Resolution Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO entity_resolution_candidates (
                    id,
                    tenant_id,
                    left_actor_key,
                    right_actor_key,
                    evidence_fingerprint,
                    idempotency_key,
                    confidence,
                    evidence_count,
                    state,
                    decision_kind,
                    decision_actor_key,
                    decision_ref,
                    canonical_actor_key,
                    created_at,
                    updated_at,
                    decided_at
                )
                VALUES (
                    'candidate-migration',
                    :tenant_id,
                    'actor-a',
                    'actor-b',
                    'fingerprint',
                    'idempotency',
                    0.9,
                    1,
                    'PROPOSED',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    now(),
                    now(),
                    NULL
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    result = migrate(
        "downgrade",
        "0051_personal_context_bootstrap",
        check=False,
    )
    assert result.returncode != 0
    assert "ENTITY_RESOLUTION_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0055_obligation_expectation_v0"
        assert connection.scalar(
            text("SELECT count(*) FROM entity_resolution_candidates")
        ) == 1
