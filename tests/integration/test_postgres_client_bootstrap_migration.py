"""Inspect the V0.3B device-bootstrap schema after Alembic migration."""

import os
import re

import pytest
from sqlalchemy import LargeBinary, String, create_engine, inspect, text


@pytest.mark.postgres
def test_client_device_bootstrap_migration_schema():
    engine = create_engine(os.environ["PUBLIC_POSTGRES_TEST_URL"], hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0046_gmail_history_cursor"]

            schema = inspect(connection)
            assert {
                "client_tenant_memberships",
                "client_devices",
                "device_bootstrap_challenges",
            } <= set(schema.get_table_names())

            membership_columns = {
                item["name"]: item for item in schema.get_columns("client_tenant_memberships")
            }
            assert set(membership_columns) == {
                "id",
                "human_identity_id",
                "tenant_id",
                "role",
                "status",
                "created_at",
                "updated_at",
            }
            assert {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("client_tenant_memberships")
            } == {
                "uq_client_membership_human_tenant": [
                    "human_identity_id",
                    "tenant_id",
                ],
            }

            device_columns = {
                item["name"]: item for item in schema.get_columns("client_devices")
            }
            assert set(device_columns) == {
                "id",
                "human_identity_id",
                "public_key_fingerprint",
                "public_key_spki",
                "canonical_name",
                "platform",
                "roles",
                "status",
                "created_at",
                "updated_at",
            }
            assert isinstance(device_columns["public_key_fingerprint"]["type"], String)
            assert device_columns["public_key_fingerprint"]["type"].length == 71
            assert isinstance(device_columns["public_key_spki"]["type"], LargeBinary)
            assert {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("client_devices")
            } == {
                "uq_client_device_public_key_fingerprint": ["public_key_fingerprint"],
            }

            challenge_columns = {
                item["name"]: item
                for item in schema.get_columns("device_bootstrap_challenges")
            }
            assert set(challenge_columns) == {
                "id",
                "continuation_grant_id",
                "human_identity_id",
                "public_key_fingerprint",
                "public_key_spki",
                "canonical_device_name",
                "platform",
                "roles",
                "challenge_digest",
                "state",
                "created_at",
                "expires_at",
                "verified_at",
                "rejected_at",
            }
            assert {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("device_bootstrap_challenges")
            } == {
                "uq_device_bootstrap_challenge_digest": ["challenge_digest"],
                "uq_device_bootstrap_continuation_grant": ["continuation_grant_id"],
            }

            forbidden = {
                "continuation_token",
                "raw_continuation_token",
                "private_key",
                "device_private_key",
                "access_token",
                "refresh_token",
                "session_id",
                "google_sub",
                "email",
            }
            assert forbidden.isdisjoint(device_columns)
            assert forbidden.isdisjoint(challenge_columns)
            assert forbidden.isdisjoint(membership_columns)

            membership_checks = {
                item["name"]: item["sqltext"]
                for item in schema.get_check_constraints("client_tenant_memberships")
            }
            assert set(re.findall(r"'([^']*)'", membership_checks["ck_client_membership_role"])) == {
                "OWNER", "ADMIN", "MEMBER",
            }
            assert set(
                re.findall(r"'([^']*)'", membership_checks["ck_client_membership_status"])
            ) == {"ACTIVE", "SUSPENDED", "REVOKED"}

            challenge_checks = {
                item["name"]: item["sqltext"]
                for item in schema.get_check_constraints("device_bootstrap_challenges")
            }
            assert set(
                re.findall(r"'([^']*)'", challenge_checks["ck_device_bootstrap_state"])
            ) == {"PENDING", "VERIFIED", "REJECTED"}

            device_fks = {
                tuple(item["constrained_columns"]): (
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                )
                for item in schema.get_foreign_keys("client_devices")
            }
            assert device_fks[("human_identity_id",)] == (
                "human_identities",
                ("id",),
            )

            membership_fks = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys("client_tenant_memberships")
            }
            assert membership_fks == {
                ("human_identity_id",): "human_identities",
                ("tenant_id",): "tenants",
            }

            challenge_fks = {
                tuple(item["constrained_columns"]): item["referred_table"]
                for item in schema.get_foreign_keys("device_bootstrap_challenges")
            }
            assert challenge_fks == {
                ("continuation_grant_id",): "human_auth_continuation_grants",
                ("human_identity_id",): "human_identities",
            }
    finally:
        engine.dispose()
