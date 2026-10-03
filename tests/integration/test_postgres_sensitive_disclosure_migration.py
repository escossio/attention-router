"""Inspect sensitive disclosure request schema."""

import os

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.mark.postgres
def test_sensitive_disclosure_migration_schema():
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
            assert "sensitive_disclosure_requests" in set(schema.get_table_names())
            columns = {
                item["name"]: item
                for item in schema.get_columns("sensitive_disclosure_requests")
            }
            assert {
                "source_interaction_id",
                "source_event_id",
                "requester_contact_id",
                "requester_relationship",
                "represented_owner_human_identity_id",
                "capability",
                "recipient_reference",
                "execution_intent_id",
                "authorization_id",
                "response_outbox_id",
                "state",
                "expires_at",
            } <= set(columns)

            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "sensitive_disclosure_requests"
                )
            }
            assert foreign_keys[("tenant_id",)] == "tenants"
            assert foreign_keys[("source_interaction_id",)] == "interactions"
            assert foreign_keys[("source_event_id",)] == "inbound_events"
            assert foreign_keys[("execution_intent_id",)] == "execution_intents"
            assert foreign_keys[("authorization_id",)] == (
                "human_execution_authorizations"
            )
            assert foreign_keys[("response_outbox_id",)] == "outbox_messages"
    finally:
        engine.dispose()
