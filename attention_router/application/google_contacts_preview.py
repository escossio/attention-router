"""Consent-scoped, ephemeral People API lookup; no Gmail binding or persistence.

The authenticated API boundary must supply the owner scope. This service is not
an HTTP endpoint and deliberately does not install or activate credentials.
"""
from dataclasses import dataclass, field
import re
from functools import wraps
from threading import RLock

from attention_router.application.gmail_connection import GoogleTokenGrant, GoogleWorkspaceOAuthClient

CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"


def canonical_phone(value: str) -> str | None:
    # No country guessing or suffix matching. Accept explicit international form.
    if not isinstance(value, str) or not re.fullmatch(r"\+[0-9 ()-]+", value):
        return None
    normalized = re.sub(r"[ ()-]", "", value)
    return normalized if re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized) else None


@dataclass
class ContactsPreviewConnection:
    tenant_id: str
    owner_id: str
    grant: GoogleTokenGrant = field(repr=False)
    active: bool = True

    @classmethod
    def connect(cls, *, tenant_id, owner_id, consent, code, oauth):
        if consent is not True or not tenant_id or not owner_id:
            raise ValueError("CONTACTS_CONSENT_REQUIRED")
        grant = oauth.exchange_authorization_code(code)
        if set(grant.granted_scopes) != {CONTACTS_SCOPE}:
            raise ValueError("CONTACTS_SCOPE_REJECTED")
        return cls(tenant_id, owner_id, grant)

    def _authorize(self, tenant_id, owner_id):
        if not self.active or (tenant_id, owner_id) != (self.tenant_id, self.owner_id):
            raise ValueError("CONTACTS_SCOPE_DENIED")

    def lookup(self, *, tenant_id, owner_id, phone, people):
        self._authorize(tenant_id, owner_id)
        number = canonical_phone(phone)
        if number is None:
            return {"status": "INVALID_PHONE"}
        # searchContacts is prefix-based; only exact canonical numbers qualify.
        people.search(self.grant.access_token, query="", limit=1)
        response = people.search(self.grant.access_token, query=number, limit=30)
        records = response.get("results", [])
        if len(records) >= 30:
            return {"status": "INCOMPLETE"}
        matches = {}
        for result in records:
            person = result.get("person", {})
            if person.get("metadata", {}).get("deleted"):
                continue
            phones = person.get("phoneNumbers", [])
            if not any(canonical_phone(p.get("canonicalForm") or p.get("value")) == number
                       for p in phones):
                continue
            resource = person.get("resourceName")
            if not resource:
                return {"status": "AMBIGUOUS"}
            if resource in matches and matches[resource] != person:
                return {"status": "AMBIGUOUS"}
            matches[resource] = person
        if len(matches) != 1:
            return {"status": "AMBIGUOUS" if matches else "NOT_FOUND"}
        resource, person = next(iter(matches.items()))
        names = {n.get("displayName") for n in person.get("names", []) if n.get("displayName")}
        if len(names) > 1:
            return {"status": "AMBIGUOUS"}
        return {"status": "MATCH", "resource": resource, "saved_as": next(iter(names), None),
                "source": "GOOGLE_CONTACTS", "identity_verified": False,
                "etag": person.get("etag"), "phone": number}

    def revoke(self, *, tenant_id, owner_id, oauth):
        self._authorize(tenant_id, owner_id)
        self.active = False  # local access stops even if provider revocation fails
        token = self.grant.refresh_token or self.grant.access_token
        self.grant = GoogleTokenGrant("", None, ())
        oauth.revoke(token)


class GooglePeopleReadOnlyClient:
    """Only limited search, no enumeration or contact mutation endpoints."""
    def __init__(self, *, opener=None, timeout=10):
        from attention_router.integrations.http_transport import urlopen_without_redirects
        self.opener = opener or urlopen_without_redirects
        self.timeout = timeout

    def search(self, access_token, *, query, limit):
        import json
        from urllib.parse import urlencode
        from urllib.request import Request
        if not 1 <= limit <= 30 or (query and canonical_phone(query) is None):
            raise ValueError("CONTACTS_QUERY_REJECTED")
        url = "https://people.googleapis.com/v1/people:searchContacts?" + urlencode({
            "query": query, "pageSize": limit, "readMask": "names,phoneNumbers,metadata",
            "sources": "READ_SOURCE_TYPE_CONTACT",
        })
        request = Request(url, headers={"Authorization": "Bearer " + access_token})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
                raise ValueError()
            return data
        except Exception:
            raise ValueError("CONTACTS_PROVIDER_UNAVAILABLE") from None


def _serialized_connection_change(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class ContactsPreviewService:
    """Ephemeral owner-only connection using existing authenticated sessions.

    Supply an OAuth client configured for the Contacts application, independently
    of Gmail. Nothing is enabled by default; grants are discarded on restart.
    """
    def __init__(self, *, client_sessions, oauth, people, enabled=False):
        self.client_sessions = client_sessions
        self.oauth = oauth
        self.people = people
        self.enabled = enabled
        self._connections = {}
        self._lock = RLock()

    def _scope(self, session, token):
        if not self.enabled:
            raise ValueError("CONTACTS_DISABLED")
        authority = self.client_sessions.authenticated_bootstrap(session, session_token=token)
        owners = [m for m in authority.memberships
                  if m.tenant_id == authority.active_tenant_id
                  and m.role == "OWNER" and m.status == "ACTIVE"]
        if len(owners) != 1:
            raise ValueError("CONTACTS_OWNER_REQUIRED")
        return authority.active_tenant_id, authority.human_identity_id

    @_serialized_connection_change
    def connect(self, session, *, session_token, consent, authorization_code):
        scope = self._scope(session, session_token)
        if scope in self._connections:
            raise ValueError("CONTACTS_ALREADY_CONNECTED")
        connection = ContactsPreviewConnection.connect(
            tenant_id=scope[0], owner_id=scope[1], consent=consent,
            code=authorization_code, oauth=self.oauth,
        )
        if scope != self._scope(session, session_token):
            raise ValueError("CONTACTS_AUTHORITY_CHANGED")
        self._connections[scope] = connection
        return {"status": "CONNECTED_EPHEMERAL", "scope": CONTACTS_SCOPE}

    @_serialized_connection_change
    def lookup(self, session, *, session_token, phone):
        scope = self._scope(session, session_token)
        connection = self._connections.get(scope)
        if connection is None:
            raise ValueError("CONTACTS_NOT_CONNECTED")
        if connection.grant.refresh_token:
            try:
                grant = self.oauth.refresh(connection.grant.refresh_token)
            except Exception:
                connection.active = False
                connection.grant = GoogleTokenGrant("", None, ())
                raise ValueError("CONTACTS_RECONNECT_REQUIRED") from None
            if set(grant.granted_scopes) != {CONTACTS_SCOPE}:
                connection.active = False
                raise ValueError("CONTACTS_SCOPE_REJECTED")
            connection.grant = grant
        result = connection.lookup(tenant_id=scope[0], owner_id=scope[1],
                                   phone=phone, people=self.people)
        if scope != self._scope(session, session_token) or not connection.active:
            raise ValueError("CONTACTS_AUTHORITY_CHANGED")
        return result

    @_serialized_connection_change
    def revoke(self, session, *, session_token):
        scope = self._scope(session, session_token)
        connection = self._connections.pop(scope, None)
        if connection:
            connection.revoke(tenant_id=scope[0], owner_id=scope[1], oauth=self.oauth)
        return {"status": "DISCONNECTED"}

    @_serialized_connection_change
    def conversation(self, session, *, session_token, source_account, identifier):
        from attention_router.application.conversation_preview import preview_selected_conversation, resolve_conversation_alias
        from attention_router.infrastructure.models import ConversationMessageRow
        from sqlalchemy import select
        scope = self._scope(session, session_token)
        view = preview_selected_conversation(session, tenant_id=scope[0],
                                             source_account=source_account, identifier=identifier)
        if view["status"] != "PREVIEW_ONLY":
            return view
        phones = set()
        for row in session.scalars(select(ConversationMessageRow).where(
            ConversationMessageRow.tenant_id == scope[0],
            ConversationMessageRow.source_account == source_account,
            ConversationMessageRow.conversation_id == view["conversation_id"],
        ).limit(1001)):
            meta = row.metadata_json or {}
            decisions = meta.get("alias_decisions", [])
            if decisions and not decisions[-1].get("active"):
                continue
            for alias in meta.get("provider_peer_aliases", []):
                if isinstance(alias, str) and alias.endswith("@c.us"):
                    phones.add("+" + alias.split("@")[0])
        phones = {phone for phone in phones if resolve_conversation_alias(
            session, tenant_id=scope[0], source_account=source_account,
            identifier=phone[1:] + "@c.us").get("conversation_key") == view["conversation_key"]}
        contact = {"status": "NOT_CONNECTED"}
        if len(phones) > 1:
            contact = {"status": "AMBIGUOUS"}
        elif len(phones) == 1 and scope in self._connections:
            contact = self.lookup(session, session_token=session_token, phone=next(iter(phones)))
        view["participants"] = [{"role": "OWNER", "basis": "AUTHENTICATED_ACCOUNT_POLICY"},
                                {"role": "PEER", "contact": contact}]
        if scope != self._scope(session, session_token):
            raise ValueError("CONTACTS_AUTHORITY_CHANGED")
        return view

    def review_alias(self, session, *, session_token, source_account, message_id, active, decision_ref):
        from attention_router.application.conversation_preview import set_alias_validity
        scope = self._scope(session, session_token)
        set_alias_validity(session, tenant_id=scope[0], source_account=source_account,
                           message_id=message_id, active=active, decision_ref=decision_ref)
        session.commit()
        return {"status": "ALIAS_REVIEWED"}

    def retry_transcription(self, session, *, session_token, transcription_id):
        from attention_router.application.voice_transcription import retry_failed_transcription
        scope = self._scope(session, session_token)
        result = retry_failed_transcription(session, tenant_id=scope[0], transcription_id=transcription_id)
        session.commit()
        return {"status": result}


class GoogleContactsOAuthClient(GoogleWorkspaceOAuthClient):
    """Independent client configuration; reuses Google's existing code exchange."""
    def refresh(self, refresh_token):
        import json
        from urllib.parse import urlencode
        from urllib.request import Request
        from attention_router.integrations.gmail_api_reader import urlopen_without_redirects
        request = Request(self.TOKEN_URL, data=urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.client_id, "client_secret": self.client_secret,
        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen_without_redirects(request, timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            data = json.loads(raw)
            if (not isinstance(data.get("access_token"), str) or not data["access_token"]
                    or data.get("token_type", "").lower() != "bearer"
                    or set(data.get("scope", CONTACTS_SCOPE).split()) != {CONTACTS_SCOPE}):
                raise ValueError()
            return GoogleTokenGrant(data["access_token"], refresh_token, (CONTACTS_SCOPE,))
        except Exception:
            raise ValueError("CONTACTS_REFRESH_REJECTED") from None
