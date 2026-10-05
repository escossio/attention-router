from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from attention_router.application.gmail_cursor_handoff import (
    GmailCursorHandoffError,
    handoff_gmail_history_cursor,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
    TenantRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.integrations.tenant_binding import INBOUND_SCOPE


NOW = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
HUMAN = "hid_cursorhandoffsynthetic000001"
ACCOUNT_HASH = "a" * 64
SOURCE = "paz_cursor_handoff_source"
DESTINATION = "paz_cursor_handoff_destination"


def _authorization(
    *,
    row_id: str,
    slot_key: str,
    tenant_id: str,
    binding_id: str,
    credential_id: str,
    status: str,
    cursor: str | None,
) -> ProviderAuthorizationRow:
    revoked = status == "REVOKED"
    return ProviderAuthorizationRow(
        id=row_id,
        slot_key=slot_key,
        tenant_id=tenant_id,
        human_identity_id=HUMAN,
        provider="GOOGLE",
        product="GMAIL",
        provider_account_hash=ACCOUNT_HASH,
        gmail_history_id=cursor,
        granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        secret_nonce_b64url="nonce-" + row_id,
        secret_ciphertext_b64url="cipher-" + row_id,
        secret_key_version="v1",
        integration_binding_id=binding_id,
        integration_credential_id=credential_id,
        status=status,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(minutes=1),
        revoked_at=NOW - timedelta(minutes=2) if revoked else None,
    )


def _seed(session):
    source_tenant = "tenant-cursor-handoff-source"
    destination_tenant = "tenant-cursor-handoff-destination"
    session.add_all(
        [
            TenantRow(
                id=source_tenant,
                slug=source_tenant,
                name="Source tenant",
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            ),
            TenantRow(
                id=destination_tenant,
                slug=destination_tenant,
                name="Destination tenant",
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            ),
            HumanIdentityRow(id=HUMAN, created_at=NOW),
        ]
    )
    source_binding = IntegrationBindingRow(
        id="gmail-bind-source",
        audience="andy-integration-ingress",
        tenant_id=source_tenant,
        kind="CHANNEL",
        name="channel.email",
        instance_id="gmail-source",
        account_key="sha256:" + ACCOUNT_HASH,
        active=False,
        scopes=[INBOUND_SCOPE],
    )
    destination_binding = IntegrationBindingRow(
        id="gmail-bind-destination",
        audience="andy-integration-ingress",
        tenant_id=destination_tenant,
        kind="CHANNEL",
        name="channel.email",
        instance_id="gmail-destination",
        account_key="sha256:" + ACCOUNT_HASH,
        active=True,
        scopes=[INBOUND_SCOPE],
    )
    source_credential = IntegrationCredentialRow(
        id="gmail-cred-source",
        digest="1" * 64,
        binding_id=source_binding.id,
        not_before=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(days=90),
        revoked=True,
        scopes=[INBOUND_SCOPE],
    )
    destination_credential = IntegrationCredentialRow(
        id="gmail-cred-destination",
        digest="2" * 64,
        binding_id=destination_binding.id,
        not_before=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(days=90),
        revoked=False,
        scopes=[INBOUND_SCOPE],
    )
    source = _authorization(
        row_id=SOURCE,
        slot_key="1" * 64,
        tenant_id=source_tenant,
        binding_id=source_binding.id,
        credential_id=source_credential.id,
        status="REVOKED",
        cursor="123456",
    )
    destination = _authorization(
        row_id=DESTINATION,
        slot_key="2" * 64,
        tenant_id=destination_tenant,
        binding_id=destination_binding.id,
        credential_id=destination_credential.id,
        status="ACTIVE",
        cursor=None,
    )
    session.add_all(
        [
            source_binding,
            destination_binding,
            source_credential,
            destination_credential,
            source,
            destination,
        ]
    )
    session.commit()
    return source, destination, source_binding, destination_binding, source_credential, destination_credential


def _handoff(session, *, apply=False):
    return handoff_gmail_history_cursor(
        session,
        source_installation_id=SOURCE,
        destination_installation_id=DESTINATION,
        apply=apply,
        now=NOW,
    )


def test_dry_run_validates_without_copying_cursor(session):
    source, destination, *_ = _seed(session)

    result = _handoff(session)

    assert result.state == "READY"
    assert result.cursor_copied is False
    assert destination.gmail_history_id is None
    assert source.gmail_history_id == "123456"


def test_apply_copies_exact_cursor_and_is_idempotent(session):
    source, destination, *_ = _seed(session)

    result = _handoff(session, apply=True)
    session.commit()

    assert result.state == "APPLIED"
    assert result.cursor_copied is True
    assert destination.gmail_history_id == source.gmail_history_id == "123456"

    replay = _handoff(session, apply=True)
    assert replay.state == "ALREADY_APPLIED"
    assert replay.cursor_copied is False
    assert destination.gmail_history_id == "123456"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda rows: (
                setattr(rows[0], "status", "ACTIVE"),
                setattr(rows[0], "revoked_at", None),
            ),
            "GMAIL_CURSOR_HANDOFF_SOURCE_NOT_REVOKED",
        ),
        (
            lambda rows: setattr(rows[2], "active", True),
            "GMAIL_CURSOR_HANDOFF_SOURCE_AUTHORITY_ACTIVE",
        ),
        (
            lambda rows: setattr(rows[4], "revoked", False),
            "GMAIL_CURSOR_HANDOFF_SOURCE_AUTHORITY_ACTIVE",
        ),
        (
            lambda rows: setattr(rows[0], "gmail_history_id", None),
            "GMAIL_CURSOR_HANDOFF_SOURCE_CURSOR_MISSING",
        ),
        (
            lambda rows: setattr(rows[0], "gmail_history_id", "not-a-cursor"),
            "GMAIL_CURSOR_HANDOFF_SOURCE_CURSOR_INVALID",
        ),
        (
            lambda rows: (
                setattr(rows[1], "status", "REVOKED"),
                setattr(rows[1], "revoked_at", NOW),
            ),
            "GMAIL_CURSOR_HANDOFF_DESTINATION_NOT_ACTIVE",
        ),
        (
            lambda rows: setattr(rows[3], "active", False),
            "GMAIL_CURSOR_HANDOFF_DESTINATION_AUTHORITY_INVALID",
        ),
        (
            lambda rows: setattr(rows[5], "revoked", True),
            "GMAIL_CURSOR_HANDOFF_DESTINATION_AUTHORITY_INVALID",
        ),
        (
            lambda rows: setattr(rows[1], "provider_account_hash", "b" * 64),
            "GMAIL_CURSOR_HANDOFF_PROVIDER_ACCOUNT_MISMATCH",
        ),
        (
            lambda rows: setattr(rows[1], "human_identity_id", "hid_other_synthetic_000001"),
            "GMAIL_CURSOR_HANDOFF_HUMAN_IDENTITY_MISMATCH",
        ),
        (
            lambda rows: setattr(rows[1], "tenant_id", rows[0].tenant_id),
            "GMAIL_CURSOR_HANDOFF_SAME_TENANT",
        ),
        (
            lambda rows: setattr(rows[1], "gmail_history_id", "999"),
            "GMAIL_CURSOR_HANDOFF_DESTINATION_CURSOR_CONFLICT",
        ),
    ],
)
def test_handoff_fails_closed(session, mutation, code):
    rows = _seed(session)
    mutation(rows)

    with pytest.raises(GmailCursorHandoffError, match=code) as caught:
        _handoff(session, apply=True)

    assert caught.value.code == code
    session.rollback()


def test_handoff_rejects_same_installation(session):
    _seed(session)

    with pytest.raises(
        GmailCursorHandoffError,
        match="GMAIL_CURSOR_HANDOFF_SAME_INSTALLATION",
    ):
        handoff_gmail_history_cursor(
            session,
            source_installation_id=SOURCE,
            destination_installation_id=SOURCE,
            apply=True,
            now=NOW,
        )
