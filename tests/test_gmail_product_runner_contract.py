from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
import traceback
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import pytest
from sqlalchemy.orm import Session

from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailConnectionService,
    GoogleGmailProfile,
    GoogleTokenGrant,
)
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationInvalid,
    GmailProductAuthorizationUnavailable,
    GmailProductBindingInvalid,
    GmailProductIngressFailed,
    GmailProductProviderUnavailable,
    GmailProductRefreshFailed,
    GmailProductRunner,
    GmailProductRunnerError,
    GmailProductSecretInvalid,
    GoogleRefreshAccessTokenClient,
)
from attention_router.config import settings
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    IntegrationBindingRow,
    IntegrationCredentialRow,
    TenantRow,
)
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.gmail_api_reader import (
    GMAIL_API_BASE,
    GMAIL_METADATA_HEADERS,
    GmailApiReader,
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_connector import (
    GmailConnectorConfig,
    GmailMessage,
    IntegrationIngressClient,
    IntegrationIngressResponse,
)
from attention_router.integrations.tenant_binding import INBOUND_SCOPE, credential_digest
from attention_router.security.provider_secrets import ProviderSecretCipher


TENANT = "gmail-product-contract-tenant"
HUMAN = "hid_gmailcontractsynthetic000001"
CONNECTED_AT = datetime(2026, 9, 21, 16, tzinfo=UTC)
NOW = CONNECTED_AT + timedelta(hours=1)
REFRESH = "synthetic-gmail-refresh-sentinel-0123456789"
ACCESS = "synthetic-gmail-access-sentinel-0123456789"
BEARER = "synthetic-ingress-bearer-sentinel-0123456789ab"
CLIENT_SECRET = "synthetic-oauth-client-secret-sentinel-0123456789"
BODY = "synthetic-private-message-body-sentinel"
ATTACHMENT = "synthetic-private-attachment-bytes-sentinel"
SECRETS = (REFRESH, ACCESS, BEARER, CLIENT_SECRET)
INGRESS_URL = "http://router.invalid/api/v1/ingress/integrations/events"


def _assert_safe(*values, caplog):
    rendered = caplog.text + "\n".join(repr(value) + str(value) for value in values)
    for value in values:
        if isinstance(value, BaseException):
            rendered += "".join(traceback.format_exception(value))
    for secret in SECRETS:
        assert secret not in rendered


class _ClientSessions:
    def authenticated_bootstrap(self, session, *, session_token):
        assert session_token == "synthetic-session-token"
        return SimpleNamespace(active_tenant_id=TENANT, human_identity_id=HUMAN)


class _ConnectionOAuth:
    def exchange_authorization_code(self, code):
        assert code == "synthetic-authorization-code"
        return GoogleTokenGrant(ACCESS, REFRESH, (GMAIL_METADATA_SCOPE,))

    def gmail_profile(self, token):
        assert token == ACCESS
        return GoogleGmailProfile("synthetic-owner@example.invalid")


@pytest.fixture
def connected(session, monkeypatch):
    monkeypatch.setattr(settings, "gmail_connect_enabled", True)
    monkeypatch.setattr(settings, "gmail_product_runner_enabled", True)
    monkeypatch.setattr(settings, "gmail_product_runner_max_results", 3)
    monkeypatch.setattr(settings, "gmail_product_runner_ingress_url", INGRESS_URL)
    monkeypatch.setattr(settings, "integration_ingress_audience", "gmail-product-test")
    monkeypatch.setattr(settings, "google_workspace_oauth_client_id", "synthetic-client-id")
    monkeypatch.setattr(settings, "google_workspace_oauth_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(
        settings,
        "provider_authorization_key_b64url",
        base64.urlsafe_b64encode(b"G" * 32).decode().rstrip("="),
    )
    monkeypatch.setattr(
        "attention_router.application.gmail_connection.secrets.token_urlsafe",
        lambda size: BEARER,
    )
    # Any unexpected network path fails offline, even in rejected-authority tests.
    _install_http_fake(monkeypatch, _unexpected_network)
    session.add(TenantRow(
        id=TENANT,
        slug=TENANT,
        name="Synthetic Gmail contract tenant",
        status="ACTIVE",
        created_at=CONNECTED_AT,
        updated_at=CONNECTED_AT,
    ))
    session.add(HumanIdentityRow(id=HUMAN, created_at=CONNECTED_AT))
    session.commit()
    view = GmailConnectionService(
        settings=settings,
        client_sessions=_ClientSessions(),
        oauth=_ConnectionOAuth(),
    ).connect(
        session,
        session_token="synthetic-session-token",
        authorization_code="synthetic-authorization-code",
        now=CONNECTED_AT,
    )
    session.commit()
    session.expunge_all()
    authorization = session.get(ProviderAuthorizationRow, view.installation_id)
    binding = session.get(IntegrationBindingRow, authorization.integration_binding_id)
    credential = session.get(IntegrationCredentialRow, authorization.integration_credential_id)
    return SimpleNamespace(authorization=authorization, binding=binding, credential=credential)


def _unexpected_network(*args, **kwargs):
    pytest.fail("Unexpected network access in offline Gmail Product Runner test")


def _install_http_fake(monkeypatch, opener):
    monkeypatch.setattr(urllib_request, "urlopen", opener)
    for module in (
        "attention_router.application.gmail_product_runner",
        "attention_router.integrations.gmail_api_reader",
        "attention_router.integrations.gmail_connector",
    ):
        monkeypatch.setattr(module + ".urlopen_without_redirects", opener)


class _Refresher:
    def __init__(self):
        self.calls = 0

    def refresh_access_token(self, refresh_token):
        assert refresh_token == REFRESH
        self.calls += 1
        return ACCESS


class _Reader:
    def __init__(self):
        self.limits = []

    def search_message_ids(self, *, query, max_results):
        assert query == ""
        self.limits.append(max_results)
        return ("synthetic-message",)

    def read_message(self, message_id):
        return GmailMessage(
            message_id=message_id,
            thread_id="synthetic-thread",
            sender="sender@example.invalid",
            to=("synthetic-owner@example.invalid",),
            cc=(),
            bcc=(),
            subject="Synthetic metadata",
            body="",
            email_ts=NOW.isoformat(),
            body_observed=False,
            attachments_observed=False,
        )


class _Ingress:
    def send(self, payload):
        return IntegrationIngressResponse(202, {"status": "accepted"})


def _runner(*, refresher=None, reader=None, ingress=None):
    return GmailProductRunner(
        settings=settings,
        token_refresher=refresher or _Refresher(),
        reader_factory=lambda token: reader or _Reader(),
        ingress_factory=lambda bearer: ingress or _Ingress(),
    )


@pytest.mark.parametrize(
    ("target", "field", "value", "error"),
    [
        ("authorization", "status", "REVOKED", GmailProductAuthorizationUnavailable),
        ("authorization", "revoked_at", NOW, GmailProductAuthorizationUnavailable),
        ("authorization", "provider", "OTHER", GmailProductAuthorizationInvalid),
        ("authorization", "product", "DRIVE", GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", [], GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", None, GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", GMAIL_METADATA_SCOPE, GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", [None], GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", [{}], GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", {GMAIL_METADATA_SCOPE: True},
         GmailProductAuthorizationInvalid),
        ("authorization", "granted_scopes", [GMAIL_METADATA_SCOPE, "gmail.modify"],
         GmailProductAuthorizationInvalid),
        ("authorization", "integration_binding_id", "missing-binding", GmailProductBindingInvalid),
        ("binding", "active", False, GmailProductBindingInvalid),
        ("binding", "tenant_id", "another-tenant", GmailProductBindingInvalid),
        ("binding", "name", "channel.chat", GmailProductBindingInvalid),
        ("binding", "kind", "CAPABILITY", GmailProductBindingInvalid),
        ("binding", "audience", "another-audience", GmailProductBindingInvalid),
        ("binding", "account_key", "sha256:" + "0" * 64, GmailProductBindingInvalid),
        ("binding", "instance_id", "another-installation", GmailProductBindingInvalid),
        ("binding", "instance_id", "", GmailProductBindingInvalid),
        ("binding", "scopes", [], GmailProductBindingInvalid),
        ("binding", "scopes", None, GmailProductBindingInvalid),
        ("binding", "scopes", [INBOUND_SCOPE, "outbound"], GmailProductBindingInvalid),
        ("authorization", "integration_credential_id", "missing-credential",
         GmailProductBindingInvalid),
        ("credential", "revoked", True, GmailProductBindingInvalid),
        ("credential", "binding_id", "another-binding", GmailProductBindingInvalid),
        ("credential", "expires_at", NOW, GmailProductBindingInvalid),
        ("credential", "expires_at", NOW - timedelta(seconds=1), GmailProductBindingInvalid),
        ("credential", "not_before", NOW + timedelta(seconds=1), GmailProductBindingInvalid),
        ("credential", "not_before", "invalid-time", GmailProductBindingInvalid),
        ("credential", "expires_at", None, GmailProductBindingInvalid),
        ("credential", "scopes", [], GmailProductBindingInvalid),
        ("credential", "scopes", None, GmailProductBindingInvalid),
        ("credential", "scopes", [INBOUND_SCOPE, "outbound"], GmailProductBindingInvalid),
    ],
)
def test_unexecutable_authority_fails_before_refresh(
    connected, session, monkeypatch, caplog, target, field, value, error,
):
    schema_impossible = (target, field) in {
        ("authorization", "provider"), ("authorization", "product"),
        ("authorization", "revoked_at"), ("authorization", "integration_binding_id"),
        ("authorization", "integration_credential_id"), ("binding", "tenant_id"),
        ("credential", "binding_id"),
    } or (field in {"not_before", "expires_at"} and not isinstance(value, datetime))
    if schema_impossible:
        # Isolated unit coverage of malformed rows that database constraints
        # already prohibit. Real persisted-authority tests keep loading intact.
        rows = {
            type(row): SimpleNamespace(**{
                column.name: getattr(row, column.name) for column in row.__table__.columns
            })
            for row in vars(connected).values()
        }
        setattr(rows[type(getattr(connected, target))], field, value)

        def load(model, identity, **kwargs):
            row = rows[model]
            return row if row.id == identity else None

        monkeypatch.setattr(session, "get", load)
    else:
        setattr(getattr(connected, target), field, value)
        if field == "status" and value == "REVOKED":
            connected.authorization.revoked_at = NOW
        session.commit()
    refresher = _Refresher()
    runner = _runner(refresher=refresher)
    with pytest.raises(error) as caught:
        runner.run_once(session, installation_id=connected.authorization.id, now=NOW)
    assert refresher.calls == 0
    _assert_safe(runner, caught.value, caplog=caplog)


@pytest.mark.parametrize(
    ("target", "field", "value", "error"),
    [
        ("authorization", "status", "REVOKED", GmailProductAuthorizationUnavailable),
        ("authorization", "granted_scopes", [GMAIL_METADATA_SCOPE, "gmail.modify"],
         GmailProductAuthorizationInvalid),
        ("binding", "active", False, GmailProductBindingInvalid),
        ("credential", "revoked", True, GmailProductBindingInvalid),
    ],
)
def test_committed_revocation_or_scope_change_overrides_cached_authority(
    connected, session, caplog, target, field, value, error,
):
    cached = getattr(connected, target)
    original = getattr(cached, field)
    with Session(bind=session.get_bind(), expire_on_commit=False) as other_session:
        fresh = other_session.get(type(cached), cached.id)
        setattr(fresh, field, value)
        if field == "status":
            fresh.revoked_at = NOW
        other_session.commit()
    assert getattr(cached, field) == original
    assert not session.dirty

    refresher = _Refresher()
    with pytest.raises(error) as caught:
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    assert refresher.calls == 0
    _assert_safe(caught.value, caplog=caplog)


@pytest.mark.parametrize("installation_id", ["missing-authorization", "", None, 42])
def test_explicit_authorization_identity_is_required(connected, session, installation_id):
    refresher = _Refresher()
    with pytest.raises(GmailProductAuthorizationUnavailable):
        _runner(refresher=refresher).run_once(
            session, installation_id=installation_id, now=NOW,
        )
    assert refresher.calls == 0


def test_authority_slot_cannot_be_substituted(connected, session):
    connected.authorization.slot_key = "0" * 64
    session.commit()
    with pytest.raises(GmailProductAuthorizationInvalid):
        _runner().run_once(session, installation_id=connected.authorization.id, now=NOW)


@pytest.mark.parametrize("field", ["secret_nonce_b64url", "secret_ciphertext_b64url",
                                  "secret_key_version"])
def test_invalid_envelope_fails_closed_without_refresh(connected, session, caplog, field):
    setattr(connected.authorization, field, "invalid-synthetic-envelope")
    session.commit()
    refresher = _Refresher()
    with pytest.raises(GmailProductSecretInvalid) as caught:
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    assert refresher.calls == 0
    _assert_safe(caught.value, caplog=caplog)


def test_envelope_uses_connection_aad_exactly(connected, session, monkeypatch):
    observed = []
    original = ProviderSecretCipher.decrypt

    def decrypt(cipher, envelope, *, aad):
        observed.append(aad)
        return original(cipher, envelope, aad=aad)

    monkeypatch.setattr(ProviderSecretCipher, "decrypt", decrypt)
    _runner().run_once(session, installation_id=connected.authorization.id, now=NOW)
    assert observed == [GmailConnectionService._aad(connected.authorization.id, TENANT, HUMAN)]


def test_envelope_from_another_authority_is_rejected(connected, session):
    foreign_envelope = ProviderSecretCipher(settings.provider_authorization_key_b64url).encrypt(
        refresh_token=REFRESH,
        integration_bearer=BEARER,
        aad=b"another-authorization|another-tenant|another-human|GOOGLE|GMAIL",
    )
    connected.authorization.secret_nonce_b64url = foreign_envelope.nonce_b64url
    connected.authorization.secret_ciphertext_b64url = foreign_envelope.ciphertext_b64url
    session.commit()
    refresher = _Refresher()
    with pytest.raises(GmailProductSecretInvalid):
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    assert refresher.calls == 0


def test_ingress_bearer_must_match_persisted_credential(connected, session, caplog):
    connected.credential.digest = credential_digest("synthetic-unrelated-bearer-" + "x" * 20)
    session.commit()
    refresher = _Refresher()
    with pytest.raises(GmailProductAuthorizationInvalid) as caught:
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    assert refresher.calls == 0
    _assert_safe(caught.value, caplog=caplog)


@pytest.mark.parametrize("bearer", ["too-short", "x" * 42 + "!", "x" * 42 + "\n"])
def test_invalid_decrypted_bearer_fails_before_refresh(connected, session, caplog, bearer):
    envelope = ProviderSecretCipher(settings.provider_authorization_key_b64url).encrypt(
        refresh_token=REFRESH,
        integration_bearer=bearer,
        aad=GmailConnectionService._aad(connected.authorization.id, TENANT, HUMAN),
    )
    connected.authorization.secret_nonce_b64url = envelope.nonce_b64url
    connected.authorization.secret_ciphertext_b64url = envelope.ciphertext_b64url
    session.commit()
    refresher = _Refresher()
    with pytest.raises(GmailProductSecretInvalid) as caught:
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    assert refresher.calls == 0
    _assert_safe(caught.value, caplog=caplog)


def test_credential_is_valid_at_not_before_including_sqlite_naive_timestamps(connected, session):
    connected.credential.not_before = NOW.replace(tzinfo=None)
    connected.credential.expires_at = (NOW + timedelta(seconds=1)).replace(tzinfo=None)
    session.commit()
    result = _runner().run_once(session, installation_id=connected.authorization.id, now=NOW)
    assert result.poll.accepted == 1


@pytest.mark.parametrize("limit", [0, -1, 101, True, False, 1.5, "3", [], {}])
def test_poll_rejects_invalid_limits_before_refresh(connected, session, limit):
    refresher = _Refresher()
    with pytest.raises(ValueError, match="GMAIL_POLL_LIMIT_OUT_OF_RANGE"):
        _runner(refresher=refresher).run_once(
            session, installation_id=connected.authorization.id, max_results=limit, now=NOW,
        )
    assert refresher.calls == 0


@pytest.mark.parametrize("limit", [1, 100, None])
def test_poll_passes_explicit_or_configured_limit(connected, session, limit):
    reader = _Reader()
    result = _runner(reader=reader).run_once(
        session, installation_id=connected.authorization.id, max_results=limit, now=NOW,
    )
    assert reader.limits == [3 if limit is None else limit]
    assert result.poll.selected == 1


class _HTTPResponse:
    def __init__(self, payload=None, *, raw=None, status=200):
        self.status = status
        self._body = raw if raw is not None else json.dumps(payload).encode()
        self.read_limits = []
        self.closed = False

    def read(self, limit=None):
        self.read_limits.append(limit)
        return self._body if limit is None else self._body[:limit]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _grant(**overrides):
    return {"access_token": ACCESS, "token_type": "Bearer", "expires_in": 3600,
            "scope": GMAIL_METADATA_SCOPE, **overrides}


def _refresh_client(opener):
    return GoogleRefreshAccessTokenClient(
        client_id="synthetic-client-id", client_secret=CLIENT_SECRET, opener=opener,
    )


def test_refresh_request_is_minimal_bounded_and_memory_only(caplog):
    response = _HTTPResponse(_grant())
    captured = []

    def opener(request, timeout):
        captured.append((request, timeout))
        return response

    client = _refresh_client(opener)
    assert client.refresh_access_token(REFRESH) == ACCESS
    request, timeout = captured[0]
    assert request.full_url == "https://oauth2.googleapis.com/token"
    assert request.get_method() == "POST"
    assert dict(request.header_items()) == {"Content-type": "application/x-www-form-urlencoded"}
    assert urllib_parse.parse_qs(request.data.decode()) == {
        "grant_type": ["refresh_token"], "refresh_token": [REFRESH],
        "client_id": ["synthetic-client-id"], "client_secret": [CLIENT_SECRET],
    }
    assert timeout == 10.0
    assert response.read_limits == [64 * 1024 + 1]
    assert response.closed
    _assert_safe(client, caplog=caplog)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_refresh_http_failure_never_exposes_provider_body_or_reason(status, caplog):
    def opener(request, timeout):
        raise urllib_error.HTTPError(
            request.full_url, status, " ".join(SECRETS), {},
            BytesIO(json.dumps({"error_description": " ".join(SECRETS)}).encode()),
        )

    with pytest.raises(GmailProductRefreshFailed) as caught:
        _refresh_client(opener).refresh_access_token(REFRESH)
    _assert_safe(caught.value, caplog=caplog)


@pytest.mark.parametrize(
    "response",
    [
        _HTTPResponse(raw=("invalid-json " + " ".join(SECRETS)).encode()),
        _HTTPResponse(raw=b"\xff"),
        _HTTPResponse(raw=b"x" * (64 * 1024 + 1)),
        _HTTPResponse([]),
        _HTTPResponse(_grant(access_token=None)),
        _HTTPResponse(_grant(access_token="")),
        _HTTPResponse(_grant(access_token="white space")),
        _HTTPResponse(_grant(access_token="invalid\ntoken")),
        _HTTPResponse(_grant(token_type="Basic")),
        _HTTPResponse(_grant(expires_in=0)),
        _HTTPResponse(_grant(expires_in=True)),
        _HTTPResponse(_grant(scope="")),
        _HTTPResponse(_grant(scope=[GMAIL_METADATA_SCOPE])),
        _HTTPResponse(_grant(scope=GMAIL_METADATA_SCOPE + " gmail.modify")),
        _HTTPResponse(_grant(), status=503),
    ],
    ids=["invalid-json", "invalid-utf8", "oversized", "non-object", "token-null",
         "token-empty", "token-space", "token-newline", "wrong-token-type", "expired",
         "boolean-expiry", "empty-scope", "wrong-scope-type", "broader-scope", "http-status"],
)
def test_invalid_refresh_response_is_sanitized(response, caplog):
    with pytest.raises(GmailProductRunnerError) as caught:
        _refresh_client(lambda request, timeout: response).refresh_access_token(REFRESH)
    _assert_safe(caught.value, caplog=caplog)


def test_refresh_response_may_omit_unchanged_scope():
    payload = _grant()
    del payload["scope"]
    client = _refresh_client(lambda request, timeout: _HTTPResponse(payload))
    assert client.refresh_access_token(REFRESH) == ACCESS


@pytest.mark.parametrize("failure", [OSError, TimeoutError, urllib_error.URLError])
def test_refresh_transport_failure_is_sanitized(failure, caplog):
    def opener(request, timeout):
        raise failure(" ".join(SECRETS))

    with pytest.raises(GmailProductRefreshFailed) as caught:
        _refresh_client(opener).refresh_access_token(REFRESH)
    _assert_safe(caught.value, caplog=caplog)


def _message_payload(message_id):
    return {
        "id": message_id,
        "threadId": "synthetic-thread",
        "internalDate": "1789981200000",
        "snippet": BODY,
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <sender@example.invalid>"},
                {"name": "To", "value": "synthetic-owner@example.invalid"},
                {"name": "Subject", "value": "Synthetic subject"},
            ],
            "body": {"data": BODY},
            "parts": [{"mimeType": "application/pdf", "filename": "synthetic.pdf",
                       "body": {"attachmentId": "synthetic-attachment", "data": ATTACHMENT}}],
        },
    }


def test_subscriber_connection_runs_real_components_and_preserves_duplicates(
    connected, session, monkeypatch, caplog,
):
    requests = []
    ingress_events = []
    refresh_requests = []
    original_envelope = connected.authorization.secret_ciphertext_b64url
    original_digest = connected.credential.digest

    def opener(request, timeout):
        assert timeout == 10.0
        requests.append(request)
        if request.full_url == GoogleRefreshAccessTokenClient.TOKEN_URL:
            refresh_requests.append(urllib_parse.parse_qs(request.data.decode()))
            return _HTTPResponse(_grant())
        if request.full_url == INGRESS_URL:
            assert request.get_method() == "POST"
            assert request.get_header("Authorization") == "Bearer " + BEARER
            ingress_events.append(json.loads(request.data))
            duplicate = len(ingress_events) > 1
            return _HTTPResponse(
                {"status": "duplicate" if duplicate else "accepted",
                 "receipt_id": "synthetic-receipt", "correlation_id": "synthetic-correlation"},
                status=200 if duplicate else 202,
            )
        assert request.full_url.startswith(GMAIL_API_BASE + "/messages")
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer " + ACCESS
        parsed = urllib_parse.urlparse(request.full_url)
        query = urllib_parse.parse_qs(parsed.query)
        if parsed.path.endswith("/messages"):
            assert query == {"labelIds": ["INBOX"], "maxResults": ["1"],
                             "includeSpamTrash": ["false"]}
            # An overlong provider response must not expand this bounded run.
            return _HTTPResponse({"messages": [{"id": "synthetic-message"},
                                                {"id": "must-not-be-read"}]})
        assert parsed.path.endswith("/messages/synthetic-message")
        assert query == {"format": ["metadata"], "metadataHeaders": list(GMAIL_METADATA_HEADERS)}
        return _HTTPResponse(_message_payload("synthetic-message"))

    _install_http_fake(monkeypatch, opener)
    runner = GmailProductRunner(settings=settings)
    accepted = runner.run_once(
        session, installation_id=connected.authorization.id, max_results=1, now=NOW,
    )
    duplicate = runner.run_once(
        session, installation_id=connected.authorization.id, max_results=1, now=NOW,
    )

    assert asdict(accepted.poll) == {"selected": 1, "accepted": 1, "duplicates": 0}
    assert asdict(duplicate.poll) == {"selected": 1, "accepted": 0, "duplicates": 1}
    assert accepted.installation_id == connected.authorization.id
    assert accepted.binding_id == connected.binding.id
    assert len(requests) == 8  # refresh, INBOX list, metadata get, neutral POST per run
    assert all(form["refresh_token"] == [REFRESH] for form in refresh_requests)
    event = ingress_events[0]
    assert event["contract_type"] == "inbound_event"
    assert event["tenant_id"] == TENANT
    assert event["source"] == {
        "kind": "CHANNEL", "name": "channel.email",
        "instance_id": connected.binding.instance_id, "account_id": connected.binding.account_key,
    }
    assert event["external_event_id"] == "synthetic-message"
    assert event["actor"]["external_actor_id"] == "sender@example.invalid"
    assert event["thread"]["external_thread_id"] == "synthetic-thread"
    assert event["payload_ref"] == {"message_ref": "gmail:synthetic-message"}
    assert event["metadata_sanitized"]["body_observed"] is False
    assert event["metadata_sanitized"]["attachments_observed"] is False
    assert "body_present" not in event["metadata_sanitized"]
    assert BODY not in json.dumps(ingress_events)
    assert ATTACHMENT not in json.dumps(ingress_events)
    assert not session.new and not session.dirty and not session.deleted
    session.expire_all()
    assert connected.authorization.secret_ciphertext_b64url == original_envelope
    assert connected.credential.digest == original_digest == credential_digest(BEARER)
    _assert_safe(runner, accepted, duplicate, asdict(accepted), ingress_events, caplog=caplog)


@pytest.mark.parametrize(
    ("stage", "error"),
    [("decrypt", GmailProductSecretInvalid), ("refresh", GmailProductRefreshFailed),
     ("reader_factory", GmailProductProviderUnavailable),
     ("reader_search", GmailProductProviderUnavailable),
     ("reader_read", GmailProductProviderUnavailable),
     ("ingress_factory", GmailProductIngressFailed), ("ingress_send", GmailProductIngressFailed)],
)
def test_runner_sanitizes_dependency_exception_surfaces(
    connected, session, monkeypatch, caplog, stage, error,
):
    def fail(*args, **kwargs):
        raise RuntimeError(" ".join(SECRETS))

    refresher, reader, ingress = _Refresher(), _Reader(), _Ingress()
    runner = _runner(refresher=refresher, reader=reader, ingress=ingress)
    if stage == "decrypt":
        monkeypatch.setattr(ProviderSecretCipher, "decrypt", fail)
    elif stage == "refresh":
        monkeypatch.setattr(refresher, "refresh_access_token", fail)
    elif stage == "reader_factory":
        monkeypatch.setattr(runner, "_reader_factory", fail)
    elif stage == "reader_search":
        monkeypatch.setattr(reader, "search_message_ids", fail)
    elif stage == "reader_read":
        monkeypatch.setattr(reader, "read_message", fail)
    elif stage == "ingress_factory":
        monkeypatch.setattr(runner, "_ingress_factory", fail)
    else:
        monkeypatch.setattr(ingress, "send", fail)
    with pytest.raises(error) as caught:
        runner.run_once(session, installation_id=connected.authorization.id, now=NOW)
    assert str(caught.value) == caught.value.code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    _assert_safe(runner, caught.value, caplog=caplog)


@pytest.mark.parametrize("stage", ["gmail", "ingress"])
def test_runner_sanitizes_real_http_component_failures(
    connected, session, monkeypatch, caplog, stage,
):
    def opener(request, timeout):
        if request.full_url == GoogleRefreshAccessTokenClient.TOKEN_URL:
            return _HTTPResponse(_grant())
        if request.full_url == INGRESS_URL:
            return _HTTPResponse({"error_code": " ".join(SECRETS)}, status=403)
        if stage == "gmail":
            raise urllib_error.HTTPError(request.full_url, 503, " ".join(SECRETS), {},
                                         BytesIO(json.dumps({"error": BEARER}).encode()))
        if urllib_parse.urlparse(request.full_url).path.endswith("/messages"):
            return _HTTPResponse({"messages": [{"id": "synthetic-message"}]})
        return _HTTPResponse(_message_payload("synthetic-message"))

    _install_http_fake(monkeypatch, opener)
    error = GmailProductProviderUnavailable if stage == "gmail" else GmailProductIngressFailed
    with pytest.raises(error) as caught:
        GmailProductRunner(settings=settings).run_once(
            session, installation_id=connected.authorization.id, now=NOW,
        )
    _assert_safe(caught.value, caplog=caplog)


def test_secret_bearing_components_have_safe_representations(connected, caplog):
    token_provider = StaticGmailAccessTokenProvider(ACCESS)
    config = GmailConnectorConfig(
        tenant_id=TENANT, instance_id="synthetic-instance", account_id=None,
        ingress_url=INGRESS_URL, ingress_bearer=BEARER,
    )
    _assert_safe(
        token_provider,
        config,
        GmailApiReader(token_provider=token_provider),
        IntegrationIngressClient(url=INGRESS_URL, bearer=BEARER),
        _refresh_client(_unexpected_network),
        GoogleTokenGrant(ACCESS, REFRESH, (GMAIL_METADATA_SCOPE,)),
        settings,
        caplog=caplog,
    )


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan"), True, "10"])
def test_refresh_rejects_unbounded_or_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="GMAIL_REFRESH_TIMEOUT_INVALID"):
        GoogleRefreshAccessTokenClient(
            client_id="synthetic-client-id", client_secret=CLIENT_SECRET,
            timeout_seconds=timeout, opener=_unexpected_network,
        )


@pytest.mark.parametrize("timeout", [1, 30])
def test_refresh_honors_bounded_timeout(timeout):
    def opener(request, timeout):
        observed.append(timeout)
        return _HTTPResponse(_grant())

    observed = []
    client = GoogleRefreshAccessTokenClient(
        client_id="synthetic-client-id", client_secret=CLIENT_SECRET,
        timeout_seconds=timeout, opener=opener,
    )
    assert client.refresh_access_token(REFRESH) == ACCESS
    assert observed == [timeout]


@pytest.mark.parametrize(
    ("client_id", "client_secret", "refresh"),
    [("", CLIENT_SECRET, REFRESH), (None, CLIENT_SECRET, REFRESH),
     ("synthetic-client-id", "", REFRESH), ("synthetic-client-id", None, REFRESH),
     ("synthetic-client-id", CLIENT_SECRET, ""), ("synthetic-client-id", CLIENT_SECRET, " "),
     ("synthetic-client-id", CLIENT_SECRET, None)],
)
def test_refresh_requires_server_configuration_and_refresh_token(
    client_id, client_secret, refresh, caplog,
):
    client = GoogleRefreshAccessTokenClient(
        client_id=client_id, client_secret=client_secret, opener=_unexpected_network,
    )
    with pytest.raises(GmailProductRefreshFailed) as caught:
        client.refresh_access_token(refresh)
    _assert_safe(client, caught.value, caplog=caplog)
