"""Inspect Personal Context V2D semantic episode schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_semantic_episode_migration_schema():
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
                "semantic_episodes",
                "semantic_episode_memberships",
            } <= set(schema.get_table_names())

            episode_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("semantic_episodes")
            }
            assert episode_uniques["uq_semantic_episode_tenant_key"] == [
                "tenant_id",
                "semantic_key",
            ]

            membership_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "semantic_episode_memberships"
                )
            }
            assert membership_uniques[
                "uq_semantic_episode_membership_member"
            ] == ["episode_id", "member_type", "member_ref"]

            episode_checks = {
                item["name"]
                for item in schema.get_check_constraints("semantic_episodes")
            }
            assert {
                "ck_semantic_episode_scope_type",
                "ck_semantic_episode_state",
                "ck_semantic_episode_confidence",
                "ck_semantic_episode_sensitivity",
                "ck_semantic_episode_activity_order",
            } <= episode_checks

            membership_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "semantic_episode_memberships"
                )
            }
            assert {
                "ck_semantic_episode_membership_type",
                "ck_semantic_episode_membership_reason",
                "ck_semantic_episode_membership_confidence",
            } <= membership_checks
    finally:
        engine.dispose()


@pytest.fixture
def semantic_episode_migration_db(pg_url):
    name = "semantic_episode_migration_" + uuid.uuid4().hex[:12]
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


def test_semantic_episode_refuses_destructive_downgrade(
    semantic_episode_migration_db,
):
    engine, migrate = semantic_episode_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000d053"

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
                "slug": "semantic-episode-migration",
                "name": "Semantic Episode Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO semantic_episodes (
                    id,
                    tenant_id,
                    episode_type,
                    semantic_key,
                    scope_type,
                    scope_ref,
                    state,
                    confidence,
                    sensitivity_class,
                    started_at,
                    last_activity_at,
                    ended_at,
                    supersedes_episode_id,
                    split_from_episode_id,
                    merged_from_episode_ids,
                    provenance,
                    created_at,
                    updated_at
                )
                VALUES (
                    'episode-migration',
                    :tenant_id,
                    'PROPERTY_MATTER',
                    'episode-key',
                    'RESOURCE',
                    'resource-1',
                    'ACTIVE',
                    1.0,
                    'PRIVATE',
                    now(),
                    now(),
                    NULL,
                    NULL,
                    NULL,
                    '[]'::jsonb,
                    '{}'::jsonb,
                    now(),
                    now()
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    result = migrate(
        "downgrade",
        "0052_entity_resolution_v0",
        check=False,
    )
    assert result.returncode != 0
    assert "SEMANTIC_EPISODE_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0055_obligation_expectation_v0"
        assert connection.scalar(
            text("SELECT count(*) FROM semantic_episodes")
        ) == 1
