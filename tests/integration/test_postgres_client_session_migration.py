"""Inspect the V0.3C client-session schema after Alembic migration."""

import os

import pytest
from sqlalchemy import String, create_engine, inspect, text


@pytest.mark.postgres
def test_client_session_authority_migration_schema():
    engine = create_engine(os.environ["PUBLIC_POSTGRES_TEST_URL"], hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
                "0045_provider_authorization_v1"
            ]
            schema = inspect(connection)
            assert {"client_session_challenges", "client_sessions"} <= set(schema.get_table_names())
            challenge_columns = {item["name"]: item for item in schema.get_columns("client_session_challenges")}
            session_columns = {item["name"]: item for item in schema.get_columns("client_sessions")}
            assert set(challenge_columns) == {
                "id", "device_id", "human_identity_id", "requested_tenant_id", "challenge_digest",
                "state", "created_at", "expires_at", "verified_at", "rejected_at",
            }
            assert set(session_columns) == {
                "id", "token_digest", "source_challenge_id", "human_identity_id", "device_id",
                "tenant_id", "state", "created_at", "expires_at", "revoked_at",
            }
            assert isinstance(session_columns["token_digest"]["type"], String)
            assert session_columns["token_digest"]["type"].length == 64
            forbidden = {"session_token", "access_token", "refresh_token", "raw_token", "private_key", "google_sub", "email"}
            assert forbidden.isdisjoint(challenge_columns)
            assert forbidden.isdisjoint(session_columns)
            uniques = {item["name"]: item["column_names"] for item in schema.get_unique_constraints("client_sessions")}
            assert uniques == {
                "uq_client_session_source_challenge": ["source_challenge_id"],
                "uq_client_session_token_digest": ["token_digest"],
            }
    finally:
        engine.dispose()
