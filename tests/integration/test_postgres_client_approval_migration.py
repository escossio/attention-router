"""Inspect Native App Approval V1A decision-evidence schema."""

import os

import pytest
from sqlalchemy import String, create_engine, inspect, text


@pytest.mark.postgres
def test_client_approval_decision_evidence_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0048_native_app_approval_v1a"]

            schema = inspect(connection)
            tables = set(schema.get_table_names())
            assert "human_approval_decision_evidence" in tables

            columns = {
                item["name"]: item
                for item in schema.get_columns(
                    "human_approval_decision_evidence"
                )
            }
            assert set(columns) == {
                "id",
                "authorization_id",
                "tenant_id",
                "decision",
                "channel",
                "approver_reference",
                "human_identity_id",
                "device_id",
                "client_session_id",
                "provider_event_reference",
                "idempotency_key",
                "decided_at",
                "created_at",
            }
            for name in (
                "decision",
                "channel",
                "approver_reference",
                "human_identity_id",
                "device_id",
                "client_session_id",
                "provider_event_reference",
                "idempotency_key",
            ):
                assert isinstance(columns[name]["type"], String)

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "human_approval_decision_evidence"
                )
            }
            assert uniques["uq_human_approval_decision_authorization"] == [
                "authorization_id"
            ]
            assert uniques["uq_human_approval_decision_idempotency"] == [
                "idempotency_key"
            ]

            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "human_approval_decision_evidence"
                )
            }
            assert foreign_keys[("authorization_id",)] == (
                "human_execution_authorizations"
            )
            assert foreign_keys[("tenant_id",)] == "tenants"
            assert ("human_identity_id",) not in foreign_keys
            assert ("device_id",) not in foreign_keys
            assert ("client_session_id",) not in foreign_keys

            forbidden = {
                "session_token",
                "token_digest",
                "access_token",
                "refresh_token",
                "request_wamid",
                "decision_inbound_wamid",
                "decision_button_id",
            }
            assert forbidden.isdisjoint(columns)
    finally:
        engine.dispose()
