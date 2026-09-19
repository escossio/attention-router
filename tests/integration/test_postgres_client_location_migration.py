"""Inspect the V0.4A client-location schema after Alembic migration."""

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
            ).scalars().all() == ["0042_client_location_snapshot"]

            schema = inspect(connection)
            assert "client_location_snapshots" in set(schema.get_table_names())
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
                "updated_at",
            }
            assert isinstance(columns["latitude"]["type"], Float)
            assert isinstance(columns["longitude"]["type"], Float)
            assert isinstance(columns["accuracy_m"]["type"], Float)
            assert isinstance(columns["precision"]["type"], String)
            assert columns["precision"]["type"].length == 16

            forbidden = {
                "session_token",
                "access_token",
                "refresh_token",
                "raw_token",
                "private_key",
                "google_sub",
                "email",
                "address",
            }
            assert forbidden.isdisjoint(columns)

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "client_location_snapshots"
                )
            }
            assert uniques == {
                "uq_client_location_device_tenant": [
                    "device_id",
                    "tenant_id",
                ]
            }
    finally:
        engine.dispose()
