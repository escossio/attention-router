"""Inspect the disposable database migrated by Alembic, without creating tables."""

import os
import re

import pytest
from sqlalchemy import DateTime, String, create_engine, inspect, text


@pytest.mark.postgres
def test_human_identity_migration_schema():
    engine = create_engine(os.environ["PUBLIC_POSTGRES_TEST_URL"], hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0038_human_identity_v1"]

            schema = inspect(connection)
            expected_columns = {
                "human_identities": {
                    "id": (64, False),
                    "created_at": (None, False),
                },
                "external_identity_bindings": {
                    "id": (64, False),
                    "provider": (32, False),
                    "subject": (255, False),
                    "human_identity_id": (64, False),
                    "created_at": (None, False),
                    "last_verified_at": (None, False),
                },
                "human_auth_transactions": {
                    "id": (64, False),
                    "provider": (32, False),
                    "nonce_digest": (64, False),
                    "state": (16, False),
                    "created_at": (None, False),
                    "expires_at": (None, False),
                    "consumed_at": (None, True),
                    "resolved_human_identity_id": (64, True),
                },
            }
            assert expected_columns.keys() <= set(schema.get_table_names())
            for table, expected in expected_columns.items():
                columns = {column["name"]: column for column in schema.get_columns(table)}
                assert columns.keys() == expected.keys()
                assert schema.get_pk_constraint(table)["constrained_columns"] == ["id"]
                for name, (length, nullable) in expected.items():
                    column = columns[name]
                    assert column["nullable"] is nullable, (table, name)
                    if length is None:
                        assert isinstance(column["type"], DateTime), (table, name)
                        assert column["type"].timezone is True, (table, name)
                    else:
                        assert isinstance(column["type"], String), (table, name)
                        assert column["type"].length == length, (table, name)

            auth_columns = {
                column["name"] for column in schema.get_columns("human_auth_transactions")
            }
            assert auth_columns.isdisjoint({
                "tenant_id", "device_id", "session_id", "nonce", "id_token", "token",
                "access_token", "refresh_token", "email", "name",
            })

            for table, expected_unique in (
                ("external_identity_bindings", {
                    "uq_external_identity_provider_subject": ["provider", "subject"],
                }),
                ("human_auth_transactions", {
                    "uq_human_auth_transaction_nonce_digest": ["nonce_digest"],
                }),
            ):
                assert {
                    item["name"]: item["column_names"]
                    for item in schema.get_unique_constraints(table)
                } == expected_unique

            for table, column in (
                ("external_identity_bindings", "human_identity_id"),
                ("human_auth_transactions", "resolved_human_identity_id"),
            ):
                foreign_keys = schema.get_foreign_keys(table)
                assert len(foreign_keys) == 1
                foreign_key = foreign_keys[0]
                assert foreign_key["constrained_columns"] == [column]
                assert foreign_key["referred_table"] == "human_identities"
                assert foreign_key["referred_columns"] == ["id"]

            checks = {
                item["name"]: item["sqltext"]
                for item in schema.get_check_constraints("human_auth_transactions")
            }
            assert checks.keys() == {"ck_human_auth_transaction_state"}
            state_check = checks["ck_human_auth_transaction_state"]
            assert set(re.findall(r"'([^']*)'", state_check)) == {
                "PENDING", "VERIFIED", "REJECTED",
            }
            # Evaluate the actual migrated CHECK, including its rejection behavior.
            for state in ("PENDING", "VERIFIED", "REJECTED", "", "pending", "UNKNOWN"):
                accepted = connection.scalar(text(
                    f"SELECT ({state_check}) FROM (VALUES (CAST(:state AS varchar))) "
                    "AS candidate(state)"
                ), {"state": state})
                assert accepted is (state in {"PENDING", "VERIFIED", "REJECTED"})
    finally:
        engine.dispose()
