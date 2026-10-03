"""Inspect Personal Context V2B semantic bootstrap schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_personal_context_bootstrap_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0054_candidate_insight_v0"]

            schema = inspect(connection)
            tables = set(schema.get_table_names())
            assert {
                "personal_context_bootstrap_runs",
                "personal_context_bootstrap_batches",
            } <= tables

            run_columns = {
                item["name"]: item
                for item in schema.get_columns(
                    "personal_context_bootstrap_runs"
                )
            }
            assert {
                "id",
                "tenant_id",
                "owner_human_identity_id",
                "represented_owner_actor_key",
                "source_kind",
                "source_account",
                "source_revision",
                "source_selection",
                "consent_ref",
                "idempotency_key",
                "mode",
                "state",
                "requested_control",
                "resume_cursor",
                "processing_budget",
                "progress",
                "failure_summary",
                "created_at",
                "updated_at",
                "started_at",
                "paused_at",
                "completed_at",
                "cancelled_at",
                "failed_at",
            } == set(run_columns)

            batch_columns = {
                item["name"]: item
                for item in schema.get_columns(
                    "personal_context_bootstrap_batches"
                )
            }
            assert {
                "id",
                "bootstrap_run_id",
                "ordinal",
                "idempotency_key",
                "state",
                "cursor_before",
                "cursor_after",
                "metrics",
                "failure_summary",
                "created_at",
                "started_at",
                "completed_at",
                "updated_at",
            } == set(batch_columns)

            run_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "personal_context_bootstrap_runs"
                )
            }
            assert run_uniques[
                "uq_personal_context_bootstrap_run_idempotency"
            ] == ["idempotency_key"]

            batch_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "personal_context_bootstrap_batches"
                )
            }
            assert batch_uniques[
                "uq_personal_context_bootstrap_batch_ordinal"
            ] == ["bootstrap_run_id", "ordinal"]
            assert batch_uniques[
                "uq_personal_context_bootstrap_batch_idempotency"
            ] == ["idempotency_key"]

            run_foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "personal_context_bootstrap_runs"
                )
            }
            assert run_foreign_keys[("tenant_id",)] == "tenants"
            assert (
                run_foreign_keys[("owner_human_identity_id",)]
                == "human_identities"
            )

            batch_foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "personal_context_bootstrap_batches"
                )
            }
            assert (
                batch_foreign_keys[("bootstrap_run_id",)]
                == "personal_context_bootstrap_runs"
            )

            run_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "personal_context_bootstrap_runs"
                )
            }
            assert {
                "ck_personal_context_bootstrap_run_state",
                "ck_personal_context_bootstrap_run_control",
                "ck_personal_context_bootstrap_consent_nonempty",
            } <= run_checks

            batch_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "personal_context_bootstrap_batches"
                )
            }
            assert {
                "ck_personal_context_bootstrap_batch_ordinal_positive",
                "ck_personal_context_bootstrap_batch_state",
            } <= batch_checks

            run_indexes = {
                item["name"]
                for item in schema.get_indexes(
                    "personal_context_bootstrap_runs"
                )
            }
            assert {
                "ix_personal_context_bootstrap_owner_state",
                "ix_personal_context_bootstrap_human_state",
            } <= run_indexes

            batch_indexes = {
                item["name"]
                for item in schema.get_indexes(
                    "personal_context_bootstrap_batches"
                )
            }
            assert {
                "ix_personal_context_bootstrap_batch_run_state"
            } <= batch_indexes
    finally:
        engine.dispose()


@pytest.fixture
def bootstrap_migration_db(pg_url):
    name = "pc_bootstrap_migration_" + uuid.uuid4().hex[:12]
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


def test_personal_context_bootstrap_refuses_destructive_downgrade(
    bootstrap_migration_db,
):
    engine, migrate = bootstrap_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000b051"
    human_id = "hid-bootstrap-migration"

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
                "slug": "bootstrap-migration",
                "name": "Bootstrap Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO human_identities (id, created_at)
                VALUES (:id, now())
                """
            ),
            {"id": human_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_context_bootstrap_runs (
                    id,
                    tenant_id,
                    owner_human_identity_id,
                    represented_owner_actor_key,
                    source_kind,
                    source_account,
                    source_revision,
                    source_selection,
                    consent_ref,
                    idempotency_key,
                    mode,
                    state,
                    requested_control,
                    resume_cursor,
                    processing_budget,
                    progress,
                    failure_summary,
                    created_at,
                    updated_at,
                    started_at,
                    paused_at,
                    completed_at,
                    cancelled_at,
                    failed_at
                )
                VALUES (
                    'bootstrap-run',
                    :tenant_id,
                    :human_id,
                    'owner-bootstrap',
                    'WHATSAPP_TEXT',
                    'primary',
                    NULL,
                    '{}'::jsonb,
                    'consent-migration',
                    'bootstrap-migration-idempotency',
                    'HISTORICAL_BOOTSTRAP',
                    'CREATED',
                    'NONE',
                    NULL,
                    '{"page_size": 50, "max_messages_per_chat": 100, "max_total_messages": 500}'::jsonb,
                    '{}'::jsonb,
                    NULL,
                    now(),
                    now(),
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL
                )
                """
            ),
            {"tenant_id": tenant_id, "human_id": human_id},
        )

    result = migrate(
        "downgrade",
        "0050_client_pending_source",
        check=False,
    )
    assert result.returncode != 0
    assert (
        "PERSONAL_CONTEXT_BOOTSTRAP_DOWNGRADE_REQUIRES_DATA_EXPORT"
        in result.stderr
    )
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0054_candidate_insight_v0"
        assert connection.scalar(
            text("SELECT count(*) FROM personal_context_bootstrap_runs")
        ) == 1
