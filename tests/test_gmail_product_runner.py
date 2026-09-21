from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
from urllib import parse as urllib_parse

import pytest

from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailConnectionService,
    GoogleGmailProfile,
    GoogleTokenGrant,
)
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationInvalid,
    GmailProductRunner,
    GmailProductRunnerDisabled,
    GoogleRefreshAccessTokenClient,
)
from attention_router.config import settings
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import IntegrationCredentialRow, TenantRow
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.gmail_connector import GmailMessage, IntegrationIngressResponse

TENANT = "gmail-runner-tenant"
HUMAN = "hid_gmailrunnersynthetic000001"
SESSION_TOKEN = "cst_" + "t" * 43

def _key():
    return base64.urlsafe_b64encode(b"R" * 32).decode().rstrip("=")


class FakeClientSessions:
    def authenticated_bootstrap(self, session, *, session_token, now=None):
        assert session_token == SESSION_TOKEN
        return type(
            "Authority",
            (),
            {"active_tenant_id": TENANT, "human_identity_id": HUMAN},
        )()


class FakeOAuth:
    def exchange_authorization_code(self, code):
        assert code == "server-auth-code"
        return GoogleTokenGrant(
            access_token="initial-access-token",
            refresh_token="stored-refresh-token",
            granted_scopes=(GMAIL_METADATA_SCOPE,),
        )

    def gmail_profile(self, access_token):
        assert access_token == "initial-access-token"
        return GoogleGmailProfile("owner@example.invalid")

    def revoke(self, refresh_token):
        raise AssertionError("revoke is not expected")


class FakeRefresher:
    def __init__(self):
        self.seen = []

    def refresh_access_token(self, refresh_token):
        self.seen.append(refresh_token)
        return "transient-access-token"

class FakeReader:
    def __init__(self, token):
        self.token = token
        self.searches = []
        self.reads = []

    def search_message_ids(self, *, query, max_results):
        self.searches.append((query, max_results))
        return ("gmail-message-1",)

    def read_message(self, message_id):
        self.reads.append(message_id)
        return GmailMessage(
            message_id=message_id,
            thread_id="gmail-thread-1",
            sender="Sender <sender@example.invalid>",
            to=("owner@example.invalid",),
            cc=(),
            bcc=(),
            subject="Metadata only",
            body="",
            email_ts="2026-09-21T17:00:00+00:00",
            attachments=(),
            body_observed=False,
            attachments_observed=False,
        )


class FakeIngress:
    def __init__(self, bearer):
        self.bearer = bearer
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return IntegrationIngressResponse(
            status_code=202,
            body={
                "status": "accepted",
                "receipt_id": "receipt-1",
                "admitted_at": "2026-09-21T17:00:01Z",
                "correlation_id": "corr-1",
            },
        )

def _seed_connected(session, monkeypatch):
    monkeypatch.setattr(settings, "gmail_connect_enabled", True)
    monkeypatch.setattr(settings, "provider_authorization_key_b64url", _key())
    monkeypatch.setattr(settings, "integration_ingress_audience", "andy-product")
    stamp = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    session.add(
        TenantRow(
            id=TENANT,
            slug=TENANT,
            name="Gmail Runner Tenant",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    session.add(HumanIdentityRow(id=HUMAN, created_at=stamp))
    session.commit()
    service = GmailConnectionService(
        settings=settings,
        client_sessions=FakeClientSessions(),
        oauth=FakeOAuth(),
    )
    connected = service.connect(
        session,
        session_token=SESSION_TOKEN,
        authorization_code="server-auth-code",
        now=stamp,
    )
    session.commit()
    return connected.installation_id


def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "gmail_product_runner_enabled", True)
    monkeypatch.setattr(settings, "gmail_product_runner_max_results", 3)
    monkeypatch.setattr(
        settings,
        "gmail_product_runner_ingress_url",
        "http://127.0.0.1:18101/api/v1/ingress/integrations/events",
    )

def test_product_runner_uses_connected_authorization_without_manual_token(
    session,
    monkeypatch,
):
    installation_id = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    refresher = FakeRefresher()
    readers = []
    ingresses = []

    def reader_factory(token):
        reader = FakeReader(token)
        readers.append(reader)
        return reader

    def ingress_factory(bearer):
        ingress = FakeIngress(bearer)
        ingresses.append(ingress)
        return ingress

    runner = GmailProductRunner(
        settings=settings,
        token_refresher=refresher,
        reader_factory=reader_factory,
        ingress_factory=ingress_factory,
    )
    result = runner.run_once(
        session,
        installation_id=installation_id,
        now=datetime(2026, 9, 21, 17, 0, tzinfo=UTC),
    )

    assert result.installation_id == installation_id
    assert result.poll.selected == 1
    assert result.poll.accepted == 1
    assert refresher.seen == ["stored-refresh-token"]
    assert readers[0].token == "transient-access-token"
    assert readers[0].searches == [("", 3)]
    assert readers[0].reads == ["gmail-message-1"]
    assert ingresses[0].bearer and ingresses[0].bearer != "stored-refresh-token"
    event = ingresses[0].payloads[0]
    assert event["tenant_id"] == TENANT
    assert event["source"]["name"] == "channel.email"
    assert event["source"]["account_id"].startswith("sha256:")
    assert event["metadata_sanitized"]["body_observed"] is False
    assert event["metadata_sanitized"]["attachments_observed"] is False
    assert "body_present" not in event["metadata_sanitized"]

def test_product_runner_fails_closed_when_disabled(session, monkeypatch):
    installation_id = _seed_connected(session, monkeypatch)
    monkeypatch.setattr(settings, "gmail_product_runner_enabled", False)
    runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher())

    with pytest.raises(GmailProductRunnerDisabled):
        runner.run_once(session, installation_id=installation_id)


def test_product_runner_rejects_broader_persisted_scope(session, monkeypatch):
    installation_id = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    row = session.get(ProviderAuthorizationRow, installation_id)
    row.granted_scopes = [
        GMAIL_METADATA_SCOPE,
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    session.commit()

    runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher())
    with pytest.raises(GmailProductAuthorizationInvalid):
        runner.run_once(session, installation_id=installation_id)


def test_product_runner_rejects_revoked_ingress_credential(session, monkeypatch):
    installation_id = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    row = session.get(ProviderAuthorizationRow, installation_id)
    credential = session.get(IntegrationCredentialRow, row.integration_credential_id)
    credential.revoked = True
    session.commit()

    runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher())
    with pytest.raises(GmailProductAuthorizationInvalid):
        runner.run_once(session, installation_id=installation_id)


class FakeTokenHTTPResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self, _limit=None):
        return json.dumps(self._payload).encode("utf-8")

def test_refresh_client_exchanges_refresh_token_in_memory():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeTokenHTTPResponse(
            {
                "access_token": "new-access-token",
                "scope": GMAIL_METADATA_SCOPE,
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )

    client = GoogleRefreshAccessTokenClient(
        client_id="client-id",
        client_secret="client-secret",
        opener=opener,
    )
    access = client.refresh_access_token("refresh-secret")

    assert access == "new-access-token"
    form = urllib_parse.parse_qs(captured["request"].data.decode("ascii"))
    assert form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-secret"],
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
    }
    assert captured["timeout"] == 10.0


def test_refresh_client_rejects_scope_above_metadata():
    def opener(_request, timeout):
        assert timeout == 10.0
        return FakeTokenHTTPResponse(
            {
                "access_token": "new-access-token",
                "scope": (
                    GMAIL_METADATA_SCOPE
                    + " https://www.googleapis.com/auth/gmail.modify"
                ),
            }
        )

    client = GoogleRefreshAccessTokenClient(
        client_id="client-id",
        client_secret="client-secret",
        opener=opener,
    )
    with pytest.raises(GmailProductAuthorizationInvalid):
        client.refresh_access_token("refresh-secret")

def test_product_runner_rejects_bearer_digest_mismatch_before_refresh(
    session,
    monkeypatch,
):
    installation_id = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    row = session.get(ProviderAuthorizationRow, installation_id)
    credential = session.get(IntegrationCredentialRow, row.integration_credential_id)
    credential.digest = "0" * 64
    session.commit()
    refresher = FakeRefresher()

    runner = GmailProductRunner(settings=settings, token_refresher=refresher)
    with pytest.raises(GmailProductAuthorizationInvalid):
        runner.run_once(session, installation_id=installation_id)

    assert refresher.seen == []
