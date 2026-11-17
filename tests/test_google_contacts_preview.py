from unittest.mock import Mock

import pytest

from attention_router.application.gmail_connection import GoogleTokenGrant
from attention_router.application.google_contacts_preview import (
    CONTACTS_SCOPE, ContactsPreviewConnection, canonical_phone,
)


def connection():
    oauth = Mock()
    oauth.exchange_authorization_code.return_value = GoogleTokenGrant("synthetic", None, (CONTACTS_SCOPE,))
    return ContactsPreviewConnection.connect(tenant_id="tenant", owner_id="owner", consent=True,
                                             code="synthetic-code", oauth=oauth), oauth


def test_consent_scope_and_revocation():
    c, oauth = connection()
    with pytest.raises(ValueError):
        c.lookup(tenant_id="other", owner_id="owner", phone="+550000000001", people=Mock())
    c.revoke(tenant_id="tenant", owner_id="owner", oauth=oauth)
    with pytest.raises(ValueError):
        c.lookup(tenant_id="tenant", owner_id="owner", phone="+550000000001", people=Mock())
    assert "synthetic" not in repr(c)
    oauth.exchange_authorization_code.return_value = GoogleTokenGrant("synthetic", None, ("gmail.readonly",))
    with pytest.raises(ValueError):
        ContactsPreviewConnection.connect(tenant_id="tenant", owner_id="owner", consent=True,
                                          code="synthetic-code", oauth=oauth)


def test_exact_phone_duplicates_and_no_guessing():
    c, _ = connection()
    people = Mock()
    person = {"resourceName": "people/synthetic", "names": [{"displayName": "Contato sintético"}],
              "phoneNumbers": [{"canonicalForm": "+550000000001"}]}
    people.search.return_value = {"results": [{"person": person}]}
    args = dict(tenant_id="tenant", owner_id="owner", phone="+55 (000) 000-0001", people=people)
    result = c.lookup(**args)
    assert result["status"] == "MATCH" and result["identity_verified"] is False
    people.search.return_value = {"results": [{"person": person},
        {"person": {**person, "resourceName": "people/duplicate"}}]}
    assert c.lookup(**args)["status"] == "AMBIGUOUS"
    assert canonical_phone("0000000001") is None


def test_people_client_is_bounded_readonly_and_sanitizes_errors():
    from io import BytesIO
    from attention_router.application.google_contacts_preview import GooglePeopleReadOnlyClient
    opener = Mock(return_value=BytesIO(b'{"results": []}'))
    client = GooglePeopleReadOnlyClient(opener=opener)
    assert client.search("synthetic", query="+550000000001", limit=30) == {"results": []}
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert "people:searchContacts" in request.full_url
    assert "readMask=names%2CphoneNumbers%2Cmetadata" in request.full_url
    opener.side_effect = RuntimeError("private provider response")
    with pytest.raises(ValueError, match="^CONTACTS_PROVIDER_UNAVAILABLE$"):
        client.search("synthetic", query="", limit=1)


def test_service_reuses_session_authority_and_never_gmail_binding():
    from types import SimpleNamespace
    from attention_router.application.google_contacts_preview import ContactsPreviewService
    sessions = Mock()
    membership = SimpleNamespace(tenant_id="tenant", role="OWNER", status="ACTIVE")
    sessions.authenticated_bootstrap.return_value = SimpleNamespace(
        active_tenant_id="tenant", human_identity_id="owner", memberships=[membership])
    _, oauth = connection()
    people = Mock()
    people.search.return_value = {"results": []}
    service = ContactsPreviewService(client_sessions=sessions, oauth=oauth, people=people)
    with pytest.raises(ValueError, match="CONTACTS_DISABLED"):
        service.connect(None, session_token="synthetic", consent=True, authorization_code="synthetic")
    service.enabled = True
    service.connect(None, session_token="synthetic", consent=True, authorization_code="synthetic")
    assert service.lookup(None, session_token="synthetic", phone="+550000000001")["status"] == "NOT_FOUND"
    membership.role = "MEMBER"
    with pytest.raises(ValueError, match="CONTACTS_OWNER_REQUIRED"):
        service.lookup(None, session_token="synthetic", phone="+550000000001")
    membership.role = "OWNER"
    service.revoke(None, session_token="synthetic")
    with pytest.raises(ValueError, match="CONTACTS_NOT_CONNECTED"):
        service.lookup(None, session_token="synthetic", phone="+550000000001")


def test_changed_deleted_contacts_and_refresh_denied():
    from types import SimpleNamespace
    from attention_router.application.google_contacts_preview import ContactsPreviewService
    sessions=Mock()
    sessions.authenticated_bootstrap.return_value=SimpleNamespace(active_tenant_id='tenant',
        human_identity_id='owner',memberships=[SimpleNamespace(tenant_id='tenant',role='OWNER',status='ACTIVE')])
    oauth=Mock()
    grant=GoogleTokenGrant('synthetic-access','synthetic-refresh',(CONTACTS_SCOPE,))
    oauth.exchange_authorization_code.return_value=grant
    oauth.refresh.return_value=grant
    people=Mock()
    person={'resourceName':'people/synthetic','names':[{'displayName':'Nome antigo'}],
            'phoneNumbers':[{'canonicalForm':'+550000000001'}]}
    people.search.return_value={'results':[{'person':person}]}
    service=ContactsPreviewService(client_sessions=sessions,oauth=oauth,people=people,enabled=True)
    service.connect(None,session_token='synthetic',consent=True,authorization_code='synthetic-code')
    kwargs={'session_token':'synthetic','phone':'+550000000001'}
    assert service.lookup(None,**kwargs)['saved_as']=='Nome antigo'
    person['names']=[{'displayName':'Nome corrigido'}]
    assert service.lookup(None,**kwargs)['saved_as']=='Nome corrigido'
    person['metadata']={'deleted':True}
    assert service.lookup(None,**kwargs)['status']=='NOT_FOUND'
    oauth.refresh.side_effect=RuntimeError('synthetic revoked')
    with pytest.raises(ValueError,match='CONTACTS_RECONNECT_REQUIRED'):
        service.lookup(None,**kwargs)


@pytest.mark.parametrize("payload,accepted", [
    ({"access_token": "synthetic", "token_type": "Bearer", "scope": CONTACTS_SCOPE}, True),
    ({"access_token": "synthetic", "token_type": "Bearer", "scope": "gmail.readonly"}, False),
    ({"error": "invalid_grant"}, False),
    ({"access_token": "synthetic", "token_type": "Basic", "scope": CONTACTS_SCOPE}, False),
])
def test_contacts_refresh_http_contract(monkeypatch, payload, accepted):
    import json
    from io import BytesIO
    from urllib.parse import parse_qs
    from attention_router.application.google_contacts_preview import GoogleContactsOAuthClient
    opener = Mock(return_value=BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr("attention_router.integrations.gmail_api_reader.urlopen_without_redirects", opener)
    client = GoogleContactsOAuthClient(client_id="synthetic-contacts", client_secret="synthetic-secret")
    if accepted:
        grant = client.refresh("synthetic-refresh")
        assert grant.granted_scopes == (CONTACTS_SCOPE,)
    else:
        with pytest.raises(ValueError, match="^CONTACTS_REFRESH_REJECTED$"):
            client.refresh("synthetic-refresh")
    request = opener.call_args.args[0]
    assert request.full_url == "https://oauth2.googleapis.com/token"
    assert parse_qs(request.data.decode())["client_id"] == ["synthetic-contacts"]
    assert parse_qs(request.data.decode())["grant_type"] == ["refresh_token"]
