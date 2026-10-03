"""Inspect Personal Context V2B semantic bootstrap schema."""

import os

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.mark.postgres
def test_personal_context_bootstrap_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0051_personal_context_bootstrap"]

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
