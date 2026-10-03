"""Inspect owner public profile schema."""

import os

import pytest
from sqlalchemy import String, create_engine, inspect, text


@pytest.mark.postgres
def test_owner_profile_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0052_sensitive_disclosure"]

            schema = inspect(connection)
            assert "human_profiles" in set(schema.get_table_names())
            columns = {
                item["name"]: item
                for item in schema.get_columns("human_profiles")
            }
            assert set(columns) == {
                "human_identity_id",
                "assistant_reference_name",
                "created_at",
                "updated_at",
            }
            assert isinstance(
                columns["assistant_reference_name"]["type"],
                String,
            )
            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys("human_profiles")
            }
            assert foreign_keys[("human_identity_id",)] == "human_identities"
    finally:
        engine.dispose()
