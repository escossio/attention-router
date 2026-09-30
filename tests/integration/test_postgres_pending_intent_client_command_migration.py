"""Inspect provider-neutral Pending Intent provenance schema."""

import os

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.mark.postgres
def test_pending_intent_client_command_source_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0050_client_pending_source"]

            schema = inspect(connection)
            columns = {
                item["name"]: item
                for item in schema.get_columns("pending_intents")
            }
            assert columns["tenant_id"]["nullable"] is False
            assert columns["source_tenant_id"]["nullable"] is False
            assert columns["source_inbound_event_id"]["nullable"] is True
            assert columns["source_interaction_id"]["nullable"] is True
            assert columns["source_client_command_id"]["nullable"] is True
            assert columns["resolution_client_command_id"]["nullable"] is True

            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys("pending_intents")
            }
            assert foreign_keys[("tenant_id",)] == "tenants"
            assert foreign_keys[("source_tenant_id",)] == "tenants"
            assert foreign_keys[("source_client_command_id",)] == (
                "client_command_messages"
            )
            assert foreign_keys[("resolution_client_command_id",)] == (
                "client_command_messages"
            )

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("pending_intents")
            }
            assert uniques["uq_pending_intent_source_client_command"] == [
                "source_client_command_id"
            ]

            checks = {
                item["name"]
                for item in schema.get_check_constraints("pending_intents")
            }
            assert "ck_pending_intent_source_exactly_one" in checks
            assert "ck_pending_intent_resolution_at_most_one" in checks
            assert "ck_pending_intent_resolved_complete" in checks

            indexes = {
                item["name"]: item
                for item in schema.get_indexes("pending_intents")
            }
            assert indexes[
                "uq_pending_intent_resolution_client_command"
            ]["unique"]
    finally:
        engine.dispose()
