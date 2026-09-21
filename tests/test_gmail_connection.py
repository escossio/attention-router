from __future__ import annotations

import base64
from datetime import UTC, datetime

from sqlalchemy import select

from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailConnectionService,
    GoogleGmailProfile,
    GoogleTokenGrant,
)
from attention_router.config import settings
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
    TenantRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.security.provider_secrets import (
    ProviderSecretCipher,
    ProviderSecretEnvelope,
)


TENANT = "gmail-product-tenant"
HUMAN = "hid_gmailproductsynthetic000001"


class FakeClientSessions:
    def authenticated_bootstrap(self, session, *, session_token, now=None):
        assert session_token == "cst_" + "t" * 43
        return type(
            "Authority",
            (),
            {
                "active_tenant_id": TENANT,
                "human_identity_id": HUMAN,
            },
        )()


class FakeOAuth:
    def __init__(self):
        self.refresh_token = "google-refresh-secret"
        self.email = "Owner@Example.Invalid"
        self.revoked = []

    def exchange_authorization_code(self, code):
        assert code == "server-auth-code"
        return GoogleTokenGrant(
            access_token="google-access-secret",
            refresh_token=self.refresh_token,
            granted_scopes=(GMAIL_METADATA_SCOPE,),
        )

    def gmail_profile(self, access_token):
        assert access_token == "google-access-secret"
        return GoogleGmailProfile(self.email.casefold())

    def revoke(self, refresh_token):
        self.revoked.append(refresh_token)


def _key():
    return base64.urlsafe_b64encode(b"K" * 32).decode().rstrip("=")


def _service(monkeypatch, oauth):
    monkeypatch.setattr(settings, "gmail_connect_enabled", True)
    monkeypatch.setattr(
        settings,
        "provider_authorization_key_b64url",
        _key(),
    )
    monkeypatch.setattr(
        settings,
        "integration_ingress_audience",
        "andy-product",
    )
    return GmailConnectionService(
        settings=settings,
        client_sessions=FakeClientSessions(),
        oauth=oauth,
    )


def _seed(session):
    stamp = datetime(2026, 9, 20, 23, 0, tzinfo=UTC)
    session.add(
        TenantRow(
            id=TENANT,
            slug=TENANT,
            name="Gmail Product Tenant",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.add(HumanIdentityRow(id=HUMAN, created_at=stamp))
    session.commit()


def test_connect_persists_only_encrypted_provider_secrets(
    session,
    monkeypatch,
):
    _seed(session)
    oauth = FakeOAuth()
    service = _service(monkeypatch, oauth)
    token = "cst_" + "t" * 43

    result = service.connect(
        session,
        session_token=token,
        authorization_code="server-auth-code",
        now=datetime(2026, 9, 20, 23, 1, tzinfo=UTC),
    )
    session.commit()

    assert result.status == "CONNECTED"
    assert result.granted_scopes == (GMAIL_METADATA_SCOPE,)

    auth = session.scalar(select(ProviderAuthorizationRow))
    assert auth is not None
    assert auth.tenant_id == TENANT
    assert auth.human_identity_id == HUMAN
    assert auth.provider == "GOOGLE"
    assert auth.product == "GMAIL"
    assert auth.status == "ACTIVE"
    assert auth.provider_account_hash != oauth.email
    assert "google-refresh-secret" not in auth.secret_ciphertext_b64url
    assert "google-access-secret" not in auth.secret_ciphertext_b64url

    binding = session.get(IntegrationBindingRow, auth.integration_binding_id)
    credential = session.get(
        IntegrationCredentialRow,
        auth.integration_credential_id,
    )
    assert binding is not None
    assert binding.tenant_id == TENANT
    assert binding.name == "channel.email"
    assert binding.active is True
    assert binding.account_key.startswith("sha256:")
    assert credential is not None
    assert credential.revoked is False

    refresh, ingress_bearer = ProviderSecretCipher(_key()).decrypt(
        ProviderSecretEnvelope(
            auth.secret_nonce_b64url,
            auth.secret_ciphertext_b64url,
            auth.secret_key_version,
        ),
        aad=service._aad(auth.id, TENANT, HUMAN),
    )
    assert refresh == "google-refresh-secret"
    assert ingress_bearer
    assert ingress_bearer not in credential.digest

    status = service.status(session, session_token=token)
    assert status.status == "CONNECTED"
    assert status.installation_id == auth.id


def test_reconnect_without_new_refresh_token_preserves_existing_secret(
    session,
    monkeypatch,
):
    _seed(session)
    oauth = FakeOAuth()
    service = _service(monkeypatch, oauth)
    token = "cst_" + "t" * 43

    first = service.connect(
        session,
        session_token=token,
        authorization_code="server-auth-code",
    )
    session.commit()
    first_row = session.get(ProviderAuthorizationRow, first.installation_id)
    old_credential_id = first_row.integration_credential_id

    oauth.refresh_token = None
    second = service.connect(
        session,
        session_token=token,
        authorization_code="server-auth-code",
    )
    session.commit()

    assert second.installation_id == first.installation_id
    current = session.get(
        ProviderAuthorizationRow,
        second.installation_id,
    )
    refresh, _bearer = ProviderSecretCipher(_key()).decrypt(
        ProviderSecretEnvelope(
            current.secret_nonce_b64url,
            current.secret_ciphertext_b64url,
            current.secret_key_version,
        ),
        aad=service._aad(current.id, TENANT, HUMAN),
    )
    assert refresh == "google-refresh-secret"
    assert current.integration_credential_id != old_credential_id
    assert session.get(
        IntegrationCredentialRow,
        old_credential_id,
    ).revoked is True


def test_account_change_disables_old_binding_and_disconnect_revokes_provider(
    session,
    monkeypatch,
):
    _seed(session)
    oauth = FakeOAuth()
    service = _service(monkeypatch, oauth)
    token = "cst_" + "t" * 43

    first = service.connect(
        session,
        session_token=token,
        authorization_code="server-auth-code",
    )
    session.commit()
    first_row = session.get(ProviderAuthorizationRow, first.installation_id)
    old_binding_id = first_row.integration_binding_id

    oauth.email = "other@example.invalid"
    oauth.refresh_token = "other-refresh"
    service.connect(
        session,
        session_token=token,
        authorization_code="server-auth-code",
    )
    session.commit()

    current = session.get(
        ProviderAuthorizationRow,
        first.installation_id,
    )
    assert current.integration_binding_id != old_binding_id
    assert session.get(IntegrationBindingRow, old_binding_id).active is False

    disconnected = service.disconnect(
        session,
        session_token=token,
        now=datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
    )
    session.commit()

    assert disconnected.status == "DISCONNECTED"
    session.refresh(current)
    assert current.status == "REVOKED"
    assert current.revoked_at is not None
    assert oauth.revoked == ["other-refresh"]
    assert session.get(
        IntegrationBindingRow,
        current.integration_binding_id,
    ).active is False
    assert session.get(
        IntegrationCredentialRow,
        current.integration_credential_id,
    ).revoked is True
