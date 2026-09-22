"""Inspect Andy Client Command Channel V1 schema."""

import os

import pytest
from sqlalchemy import String, Text, create_engine, inspect, text


@pytest.mark.postgres
def test_client_command_channel_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0049_client_command_channel_v1"]

            schema = inspect(connection)
            assert "client_command_messages" in set(schema.get_table_names())
            columns = {
                item["name"]: item
                for item in schema.get_columns("client_command_messages")
            }
            assert set(columns) == {
                "id",
                "tenant_id",
                "human_identity_id",
                "device_id",
                "client_session_id",
                "client_request_id",
                "modality",
                "input_text",
                "state",
                "normalized_action",
                "response_text",
                "error_code",
                "created_at",
                "processed_at",
            }
            for name in (
                "human_identity_id",
                "device_id",
                "client_session_id",
                "client_request_id",
                "modality",
                "state",
                "normalized_action",
                "error_code",
            ):
                assert isinstance(columns[name]["type"], String)
            assert isinstance(columns["input_text"]["type"], Text)
            assert isinstance(columns["response_text"]["type"], Text)

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "client_command_messages"
                )
            }
            assert uniques["uq_client_command_request"] == [
                "tenant_id",
                "human_identity_id",
                "client_request_id",
            ]

            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "client_command_messages"
                )
            }
            assert foreign_keys[("tenant_id",)] == "tenants"
            for raw_identity in (
                ("human_identity_id",),
                ("device_id",),
                ("client_session_id",),
            ):
                assert raw_identity not in foreign_keys

            forbidden = {
                "session_token",
                "token_digest",
                "access_token",
                "refresh_token",
                "request_wamid",
                "provider_event_reference",
            }
            assert forbidden.isdisjoint(columns)
    finally:
        engine.dispose()
