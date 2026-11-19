import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from fastapi import FastAPI
from fastapi.testclient import TestClient
from attention_router.application.google_contacts import (
    ContactsService,
    ContactsProviderError,
    CONTACTS_SCOPE,
)
from attention_router.application.gmail_connection import GoogleTokenGrant
from attention_router.infrastructure.google_contacts_models import (
    GoogleContactsConnectionRow as Connection,
    GoogleContactRow as Contact,
)
from attention_router.security.provider_secrets import ProviderSecretCipher
from attention_router.api.v1.google_contacts import build_google_contacts_router


@pytest.fixture
def setup(session):
    sessions = Mock()
    sessions.authenticated_bootstrap.return_value = SimpleNamespace(
        active_tenant_id="tenant",
        human_identity_id="owner",
        memberships=[SimpleNamespace(tenant_id="tenant", role="OWNER", status="ACTIVE")],
    )
    oauth = Mock()
    oauth.exchange_authorization_code.return_value = GoogleTokenGrant(
        "access", "secret-refresh", (CONTACTS_SCOPE,)
    )
    oauth.refresh.return_value = oauth.exchange_authorization_code.return_value
    people = Mock()
    now = [1000.0]
    service = ContactsService(
        client_sessions=sessions,
        oauth=oauth,
        people=people,
        cipher=ProviderSecretCipher(base64.urlsafe_b64encode(b"k" * 32).decode()),
        enabled=True,
        now=lambda: now[0],
    )
    service.connect(
        session, session_token="test", consent=True, authorization_code="synthetic-code"
    )
    return service, oauth, people, now


def person(
    resource="people/1",
    name="Álvaro Teste",
    phone="+55 (11) 90000-0001",
    email="TEST@example.invalid",
):
    return {
        "resourceName": resource,
        "etag": "v1",
        "names": [{"displayName": name}],
        "phoneNumbers": [{"value": phone}, {"value": "900000001"}],
        "emailAddresses": [{"value": email}],
    }


def test_pagination_restart_dedup_normalized_search_and_encryption(session, setup):
    service, oauth, people, now = setup
    people.page.side_effect = [
        {"connections": [person(), person()], "nextPageToken": "page2"},
        {"connections": [person("people/2", phone="+55 11 90000-0002")], "nextSyncToken": "sync1"},
    ]
    assert service.sync_one(session)
    assert (
        service.lookup(session, session_token="test", phone="+5511900000001")["status"]
        == "INCOMPLETE"
    )
    row = session.scalar(select(Connection))
    assert "secret-refresh" not in json.dumps(row.envelope)
    # A new service instance resumes persisted paging, with no memory cursor.
    restarted = ContactsService(
        client_sessions=service.client_sessions,
        oauth=oauth,
        people=people,
        cipher=service.cipher,
        enabled=True,
        now=lambda: now[0],
    )
    now[0] += 31
    assert restarted.sync_one(session)
    assert people.page.call_args.kwargs["page"] == "page2"
    assert session.scalar(select(func.count()).select_from(Contact)) == 2
    result = restarted.lookup(session, session_token="test", phone="+5511900000001")
    assert result["status"] == "MATCH" and result["identity_verified"] is False
    assert restarted.search(session, session_token="test", query="alvaro")["status"] == "OK"
    assert (
        len(
            restarted.search(
                session, session_token="test", query="test@example.invalid", kind="email"
            )["contacts"]
        )
        == 2
    )


def test_incremental_updates_deletes_and_ambiguity(session, setup):
    service, _, people, now = setup
    people.page.return_value = {
        "connections": [person(), person("people/2")],
        "nextSyncToken": "s1",
    }
    service.sync_one(session)
    assert (
        service.lookup(session, session_token="test", phone="+5511900000001")["status"]
        == "AMBIGUOUS"
    )
    people.page.return_value = {
        "connections": [
            {"resourceName": "people/2", "metadata": {"deleted": True}},
            person(name="Novo"),
        ],
        "nextSyncToken": "s2",
    }
    now[0] += 901
    assert service.sync_one(session)
    assert people.page.call_args.kwargs["sync"] == "s1"
    assert (
        service.lookup(session, session_token="test", phone="+5511900000001")["saved_as"] == "Novo"
    )
    assert session.get(Contact, (service.key(("tenant", "owner")), "people/2")).deleted


def test_expired_sync_full_rebuild_removes_unseen_only_after_last_page(session, setup):
    service, _, people, now = setup
    people.page.return_value = {
        "connections": [person(), person("people/2")],
        "nextSyncToken": "old",
    }
    service.sync_one(session)
    now[0] += 901
    people.page.side_effect = ContactsProviderError("SYNC_EXPIRED")
    assert not service.sync_one(session)
    now[0] += 31
    people.page.side_effect = None
    people.page.return_value = {"connections": [person()], "nextPageToken": "p2"}
    assert service.sync_one(session)
    assert not session.get(Contact, (service.key(("tenant", "owner")), "people/2")).deleted
    now[0] += 31
    people.page.return_value = {"connections": [], "nextSyncToken": "new"}
    assert service.sync_one(session)
    assert session.get(Contact, (service.key(("tenant", "owner")), "people/2")).deleted


@pytest.mark.parametrize("code", ["RATE_LIMITED", "PROVIDER_UNAVAILABLE", "AUTH_REQUIRED"])
def test_backoff_and_google_revocation(session, setup, code):
    service, oauth, _, now = setup
    oauth.refresh.side_effect = ContactsProviderError(code)
    assert not service.sync_one(session)
    row = session.scalar(select(Connection))
    assert row.error == code and row.next_run > now[0]
    assert row.status == ("AUTH_REQUIRED" if code == "AUTH_REQUIRED" else "ACTIVE")
    assert not service.sync_one(session)
    assert oauth.refresh.call_count == 1


def test_scope_isolation_disconnect_clears_secrets_and_cache(session, setup):
    service, oauth, people, _ = setup
    people.page.return_value = {"connections": [person()], "nextSyncToken": "sync"}
    service.sync_one(session)
    owner = service.client_sessions.authenticated_bootstrap.return_value
    owner.human_identity_id = "another"
    with pytest.raises(ValueError):
        service.search(session, session_token="test", query="alvaro")
    owner.human_identity_id = "owner"
    service.revoke(session, session_token="test")
    assert session.scalar(select(Connection)).envelope == {}
    assert session.scalar(select(func.count()).select_from(Contact)) == 0
    oauth.revoke.assert_not_called()  # shared Google grant must not revoke Gmail
    assert not service.sync_one(session)


def test_contacts_routes_require_session_and_owner(session, setup):
    service, _, _, _ = setup
    app = FastAPI()
    app.include_router(build_google_contacts_router(get_session=lambda: session, service=service))
    client = TestClient(app)
    path = "/api/v1/integrations/contacts"
    assert client.get(path).status_code == 401
    assert client.post(path + "/search", json={"query": "test"}).status_code == 401
    client.headers["Authorization"] = "Bearer test"
    assert client.get(path).json()["status"] == "ACTIVE"
    service.client_sessions.authenticated_bootstrap.return_value.memberships[0].role = "MEMBER"
    assert client.post(path + "/search", json={"query": "test"}).status_code == 409


def test_wrong_scope_and_no_refresh_rejected(session, setup):
    service, oauth, _, _ = setup
    service.revoke(session, session_token="test")
    for grant in [
        GoogleTokenGrant("a", None, (CONTACTS_SCOPE,)),
        GoogleTokenGrant("a", "r", ("gmail",)),
    ]:
        oauth.exchange_authorization_code.return_value = grant
        with pytest.raises(ValueError):
            service.connect(
                session, session_token="test", consent=True, authorization_code="synthetic"
            )
    assert session.scalar(select(Connection)).status == "DISCONNECTED"


def test_invalid_code_is_never_echoed_and_disconnected_status_is_explicit(session, setup):
    service, _, _, _ = setup
    service.revoke(session, session_token="test")
    app = FastAPI()
    app.include_router(build_google_contacts_router(get_session=lambda: session, service=service))
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test"
    response = client.post(
        "/api/v1/integrations/contacts",
        json={"consent": True, "authorization_code": "private-code-" * 900},
    )
    assert response.status_code == 422
    assert "private-code" not in response.text
    assert client.get("/api/v1/integrations/contacts").json()["status"] == "DISCONNECTED"


def test_contact_name_or_phone_suffix_never_confers_whatsapp_identity(session, setup):
    service, _, people, _ = setup
    people.page.return_value = {"connections": [person()], "nextSyncToken": "s1"}
    service.sync_one(session)
    result = service.match_whatsapp(session, session_token="test", phone="+5511900000001")
    assert result["status"] == "NOT_FOUND" and result["identity_verified"] is False
    assert (
        service.lookup(session, session_token="test", phone="+11900000001")["status"] == "NOT_FOUND"
    )


def test_people_http_contract_is_readonly_paginated_and_bounded():
    from io import BytesIO
    from urllib.parse import urlparse, parse_qs
    from attention_router.application.google_contacts import PeopleConnections

    opener = Mock(return_value=BytesIO(b'{"connections":[],"nextSyncToken":"next"}'))
    assert (
        PeopleConnections(opener).page("synthetic-access", sync="cursor", page="page")[
            "nextSyncToken"
        ]
        == "next"
    )
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert urlparse(request.full_url).netloc == "people.googleapis.com"
    params = parse_qs(urlparse(request.full_url).query)
    assert params["syncToken"] == ["cursor"] and params["pageToken"] == ["page"]
    assert params["pageSize"] == ["100"]
    assert request.get_header("Authorization") == "Bearer synthetic-access"
    assert "synthetic-access" not in request.full_url


@pytest.mark.parametrize(
    "status,body,code",
    [
        (400, b'{"error":"invalid_grant"}', "AUTH_REQUIRED"),
        (400, b'{"error":{"details":[{"reason":"EXPIRED_SYNC_TOKEN"}]}}', "SYNC_EXPIRED"),
        (403, b"private-provider-error", "PROVIDER_UNAVAILABLE"),
        (429, b"private-provider-error", "RATE_LIMITED"),
    ],
)
def test_provider_errors_do_not_expose_response(status, body, code):
    from io import BytesIO
    from urllib.error import HTTPError
    from urllib.request import Request
    from attention_router.application.google_contacts import read_json

    opener = Mock(
        side_effect=HTTPError("https://example.invalid", status, "private", {}, BytesIO(body))
    )
    with pytest.raises(ContactsProviderError, match=code) as error:
        read_json(Request("https://example.invalid"), opener, 10)
    assert "private" not in str(error.value)


def test_contacts_code_exchange_accepts_readonly_without_gmail_scope(monkeypatch):
    from io import BytesIO
    from urllib.parse import parse_qs
    from attention_router.application.google_contacts import ContactsOAuth

    payload = {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "token_type": "Bearer",
        "scope": CONTACTS_SCOPE,
    }
    opener = Mock(return_value=BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(
        "attention_router.application.google_contacts.urlopen_without_redirects", opener
    )
    grant = ContactsOAuth(
        client_id="synthetic-client", client_secret="synthetic-secret"
    ).exchange_authorization_code("synthetic-code")
    assert grant.granted_scopes == (CONTACTS_SCOPE,)
    request = opener.call_args.args[0]
    assert parse_qs(request.data.decode())["grant_type"] == ["authorization_code"]
    assert request.full_url == "https://oauth2.googleapis.com/token"
    assert "synthetic-code" not in request.full_url
    payload["scope"] = "https://www.googleapis.com/auth/gmail.readonly"
    opener.return_value = BytesIO(json.dumps(payload).encode())
    with pytest.raises(ContactsProviderError):
        ContactsOAuth(
            client_id="synthetic-client", client_secret="synthetic-secret"
        ).exchange_authorization_code("synthetic-code")
