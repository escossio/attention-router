from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from attention_router.api.v1.contacts_preview import build_contacts_preview_router
from attention_router.application.gmail_connection import GoogleTokenGrant
from attention_router.application.google_contacts_preview import CONTACTS_SCOPE, ContactsPreviewService


def test_authenticated_contacts_connect_refresh_lookup_revoke():
    sessions = Mock()
    sessions.authenticated_bootstrap.return_value = SimpleNamespace(active_tenant_id='synthetic',
        human_identity_id='owner', memberships=[SimpleNamespace(tenant_id='synthetic', role='OWNER', status='ACTIVE')])
    oauth = Mock()
    grant = GoogleTokenGrant('synthetic-access', 'synthetic-refresh', (CONTACTS_SCOPE,))
    oauth.exchange_authorization_code.return_value = grant
    oauth.refresh.return_value = grant
    people = Mock()
    people.search.return_value = {'results': []}
    service = ContactsPreviewService(client_sessions=sessions, oauth=oauth, people=people, enabled=True)
    app = FastAPI()
    app.include_router(build_contacts_preview_router(get_session=lambda: None, service=service))
    client = TestClient(app)
    path = '/api/v1/integrations/contacts'
    assert client.post(path, json={'authorization_code':'synthetic-code','consent':True}).status_code == 401
    client.headers['Authorization'] = 'Bearer cst_' + 's'*43
    assert client.post(path, json={'authorization_code':'synthetic-code','consent':False}).status_code == 409
    assert client.post(path, json={'authorization_code':'synthetic-code','consent':True}).status_code == 200
    assert client.post(path+'/lookup', json={'phone':'+550000000025'}).json()['status'] == 'NOT_FOUND'
    oauth.refresh.assert_called_once_with('synthetic-refresh')
    assert client.delete(path).status_code == 200
    assert client.post(path+'/lookup', json={'phone':'+550000000025'}).status_code == 409
