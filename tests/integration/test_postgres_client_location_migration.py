"""Inspect the V0.4A current-location schema after Alembic migration."""

import os

import pytest
from sqlalchemy import Float, String, create_engine, inspect, text


@pytest.mark.postgres
def test_client_location_snapshot_migration_schema():
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
            tables = set(schema.get_table_names())
            assert "client_location_snapshots" in tables
            assert "client_location_history" not in tables
            assert "client_location_events" not in tables

            columns = {
                item["name"]: item
                for item in schema.get_columns("client_location_snapshots")
            }
            assert set(columns) == {
                "id",
                "human_identity_id",
                "device_id",
                "tenant_id",
                "latitude",
                "longitude",
                "accuracy_m",
                "precision",
                "captured_at",
                "received_at",
            }
            assert isinstance(columns["latitude"]["type"], Float)
            assert isinstance(columns["longitude"]["type"], Float)
            assert isinstance(columns["accuracy_m"]["type"], Float)
            assert isinstance(columns["precision"]["type"], String)

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "client_location_snapshots"
                )
            }
            assert uniques["uq_client_location_device_tenant"] == [
                "device_id",
                "tenant_id",
            ]

            foreign_keys = {
                item["name"]: (
                    item["constrained_columns"],
                    item["referred_table"],
                    item["referred_columns"],
                )
                for item in schema.get_foreign_keys(
                    "client_location_snapshots"
                )
            }
            assert foreign_keys["fk_client_location_human_identity"] == (
                ["human_identity_id"],
                "human_identities",
                ["id"],
            )
            assert foreign_keys["fk_client_location_device"] == (
                ["device_id"],
                "client_devices",
                ["id"],
            )
            assert foreign_keys["fk_client_location_tenant"] == (
                ["tenant_id"],
                "tenants",
                ["id"],
            )

            forbidden = {
                "session_token",
                "token_digest",
                "access_token",
                "refresh_token",
                "raw_token",
                "address",
                "speed",
                "bearing",
                "altitude",
            }
            assert forbidden.isdisjoint(columns)
    finally:
        engine.dispose()
