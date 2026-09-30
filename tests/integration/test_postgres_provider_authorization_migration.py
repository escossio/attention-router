"""Inspect provider authorization storage after Alembic migration."""

import os

import pytest
from sqlalchemy import String, create_engine, inspect, text


@pytest.mark.postgres
def test_provider_authorization_v1_schema():
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
                for item in schema.get_columns("provider_authorizations")
            }
            assert set(columns) == {
                "id",
                "slot_key",
                "tenant_id",
                "human_identity_id",
                "provider",
                "product",
                "provider_account_hash",
                "gmail_history_id",
                "granted_scopes",
                "secret_nonce_b64url",
                "secret_ciphertext_b64url",
                "secret_key_version",
                "integration_binding_id",
                "integration_credential_id",
                "status",
                "created_at",
                "updated_at",
                "revoked_at",
            }
            assert isinstance(columns["secret_ciphertext_b64url"]["type"], String)
            forbidden = {
                "authorization_code",
                "access_token",
                "refresh_token",
                "integration_bearer",
                "email",
                "google_sub",
                "client_secret",
            }
            assert forbidden.isdisjoint(columns)

            uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "provider_authorizations"
                )
            }
            assert uniques["uq_provider_authorization_slot"] == ["slot_key"]

            foreign_keys = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys(
                    "provider_authorizations"
                )
            }
            assert foreign_keys[("tenant_id",)] == "tenants"
            assert foreign_keys[("human_identity_id",)] == "human_identities"
            assert foreign_keys[("integration_binding_id",)] == "integration_bindings"
            assert foreign_keys[("integration_credential_id",)] == "integration_credentials"
    finally:
        engine.dispose()
