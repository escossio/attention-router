"""Persistent, owner-scoped read-only Contacts, reusing existing OAuth and AEAD."""

import hashlib
import json
import time
import unicodedata
from dataclasses import asdict
from uuid import uuid4
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request
from sqlalchemy import select, delete, cast, Text
from attention_router.application.google_contacts_preview import (
    CONTACTS_SCOPE,
    ContactsPreviewService,
    GoogleContactsOAuthClient,
    canonical_phone,
)
from attention_router.application.gmail_connection import GoogleTokenGrant
from attention_router.security.provider_secrets import ProviderSecretEnvelope
from attention_router.infrastructure.google_contacts_models import (
    GoogleContactsConnectionRow as Connection,
    GoogleContactRow as Contact,
)
from attention_router.integrations.http_transport import urlopen_without_redirects


class ContactsProviderError(ValueError):
    pass


def fold(value):
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )


def read_json(request, opener, timeout, maximum=2 * 1024 * 1024):
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(maximum + 1)
    except HTTPError as exc:
        if exc.code == 410:
            raise ContactsProviderError("SYNC_EXPIRED") from None
        if exc.code == 401:
            raise ContactsProviderError("AUTH_REQUIRED") from None
        if exc.code == 400:
            try:
                code = json.loads(exc.read(65536)).get("error")
            except Exception:
                code = None
            if code == "invalid_grant":
                raise ContactsProviderError("AUTH_REQUIRED") from None
            if isinstance(code, dict) and any(
                d.get("reason") == "EXPIRED_SYNC_TOKEN"
                for d in code.get("details", [])
                if isinstance(d, dict)
            ):
                raise ContactsProviderError("SYNC_EXPIRED") from None
        raise ContactsProviderError(
            "RATE_LIMITED" if exc.code == 429 else "PROVIDER_UNAVAILABLE"
        ) from None
    except Exception:
        raise ContactsProviderError("PROVIDER_UNAVAILABLE") from None
    if len(body) > maximum:
        raise ContactsProviderError("RESPONSE_TOO_LARGE")
    try:
        result = json.loads(body)
    except Exception:
        raise ContactsProviderError("INVALID_RESPONSE") from None
    if not isinstance(result, dict):
        raise ContactsProviderError("INVALID_RESPONSE")
    return result


class ContactsOAuth(GoogleContactsOAuthClient):
    def exchange_authorization_code(self, code):
        # Gmail's inherited exchange validates Gmail scopes. Keep that validator
        # unchanged and use the same client/endpoint with Contacts-only validation.
        data = read_json(
            Request(
                self.TOKEN_URL,
                data=urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": "",
                    }
                ).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            urlopen_without_redirects,
            self.timeout,
            65536,
        )
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        scope = data.get("scope")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not isinstance(scope, str)
            or set(scope.split()) != {CONTACTS_SCOPE}
            or data.get("token_type", "").lower() != "bearer"
        ):
            raise ContactsProviderError("AUTH_REQUIRED")
        return GoogleTokenGrant(access, refresh, (CONTACTS_SCOPE,))

    def refresh(self, refresh_token):
        data = read_json(
            Request(
                self.TOKEN_URL,
                data=urlencode(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    }
                ).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            urlopen_without_redirects,
            self.timeout,
            65536,
        )
        if not data.get("access_token") or set(data.get("scope", CONTACTS_SCOPE).split()) != {
            CONTACTS_SCOPE
        }:
            raise ContactsProviderError("AUTH_REQUIRED")
        return GoogleTokenGrant(
            data["access_token"], data.get("refresh_token") or refresh_token, (CONTACTS_SCOPE,)
        )


class PeopleConnections:
    def __init__(self, opener=urlopen_without_redirects):
        self.opener = opener

    def page(self, access_token, *, sync=None, page=None):
        params = {
            "personFields": "names,phoneNumbers,emailAddresses,organizations,metadata",
            "pageSize": 100,
            "requestSyncToken": "true",
            "sources": "READ_SOURCE_TYPE_CONTACT",
        }
        if sync:
            params["syncToken"] = sync
        if page:
            params["pageToken"] = page
        data = read_json(
            Request(
                "https://people.googleapis.com/v1/people/me/connections?" + urlencode(params),
                headers={"Authorization": "Bearer " + access_token},
            ),
            self.opener,
            10,
        )
        if not isinstance(data.get("connections", []), list):
            raise ContactsProviderError("INVALID_RESPONSE")
        return data


class ContactsService(ContactsPreviewService):
    def __init__(self, *, client_sessions, oauth, people, cipher, enabled=False, now=time.time):
        super().__init__(
            client_sessions=client_sessions, oauth=oauth, people=people, enabled=enabled
        )
        self.cipher = cipher
        self.now = now

    @staticmethod
    def key(scope):
        return hashlib.sha256(("google-contacts:" + ":".join(scope)).encode()).hexdigest()

    @staticmethod
    def aad(row):
        return f"contacts:v1:{row.id}:{row.tenant_id}:{row.owner_id}".encode()

    def pack(self, row, data):
        row.envelope = asdict(
            self.cipher.encrypt(
                refresh_token=json.dumps(data), integration_bearer="contacts-v1", aad=self.aad(row)
            )
        )

    def unpack(self, row):
        value, _ = self.cipher.decrypt(ProviderSecretEnvelope(**row.envelope), aad=self.aad(row))
        return json.loads(value)

    def connection(self, session, session_token, *, lock=False):
        scope = self._scope(session, session_token)
        q = select(Connection).where(Connection.id == self.key(scope))
        if lock:
            q = q.with_for_update()
        row = session.scalar(q)
        if row is None:
            raise ValueError("CONTACTS_NOT_CONNECTED")
        return row

    def connect(self, session, *, session_token, consent, authorization_code):
        scope = self._scope(session, session_token)
        if consent is not True:
            raise ValueError("CONTACTS_CONSENT_REQUIRED")
        row = session.scalar(
            select(Connection).where(Connection.id == self.key(scope)).with_for_update()
        )
        if row is not None and row.status == "ACTIVE":
            raise ValueError("CONTACTS_ALREADY_CONNECTED")
        grant = self.oauth.exchange_authorization_code(authorization_code)
        if set(grant.granted_scopes) != {CONTACTS_SCOPE} or not grant.refresh_token:
            raise ValueError("CONTACTS_OFFLINE_READONLY_GRANT_REQUIRED")
        if scope != self._scope(session, session_token):
            raise ValueError("CONTACTS_AUTHORITY_CHANGED")
        if row is None:
            row = Connection(
                id=self.key(scope),
                tenant_id=scope[0],
                owner_id=scope[1],
                status="ACTIVE",
                envelope={},
                generation=uuid4().hex,
                next_run=0,
                failures=0,
            )
            session.add(row)
        else:
            session.execute(delete(Contact).where(Contact.connection_id == row.id))
        row.status = "ACTIVE"
        row.next_run = 0
        row.error = None
        row.failures = 0
        row.last_sync = None
        row.generation = uuid4().hex
        self.pack(row, {"refresh": grant.refresh_token, "sync": None, "page": None})
        session.commit()
        return {"status": "CONNECTED", "scope": CONTACTS_SCOPE, "sync_status": "PENDING"}

    def status(self, session, *, session_token):
        scope = self._scope(session, session_token)
        row = session.get(Connection, self.key(scope))
        if row is None:
            return {"status": "DISCONNECTED", "scope": CONTACTS_SCOPE}
        secret = self.unpack(row) if row.status == "ACTIVE" else {}
        return {
            "status": row.status,
            "scope": CONTACTS_SCOPE,
            "last_sync": row.last_sync,
            "next_sync": row.next_run,
            "error": row.error,
            "sync_status": "ERROR"
            if row.error
            else (
                "SYNCING" if secret.get("page") else "PENDING" if row.last_sync is None else "READY"
            ),
        }

    def request_sync(self, session, *, session_token):
        row = self.connection(session, session_token, lock=True)
        if row.status != "ACTIVE":
            raise ValueError("CONTACTS_RECONNECT_REQUIRED")
        # Never bypass a provider backoff through repeated client requests.
        if not row.failures:
            row.next_run = 0
        session.commit()
        return {"status": "SCHEDULED"}

    def revoke(self, session, *, session_token):
        row = self.connection(session, session_token, lock=True)
        row.status = "DISCONNECTED"
        row.envelope = {}
        row.error = None
        session.execute(delete(Contact).where(Contact.connection_id == row.id))
        session.commit()
        # Shared Google client: local disconnect must not revoke Gmail grants.
        return {"status": "DISCONNECTED", "provider_revocation": "GOOGLE_ACCOUNT_PERMISSIONS"}

    def search(self, session, *, session_token, query, kind="name", limit=30):
        row = self.connection(session, session_token)
        if row.status != "ACTIVE":
            raise ValueError("CONTACTS_RECONNECT_REQUIRED")
        q = select(Contact).where(Contact.connection_id == row.id, Contact.deleted.is_(False))
        if kind == "name":
            term = fold(query)
            q = q.where(Contact.search_name.contains(term, autoescape=True))
        elif kind == "phone":
            term = canonical_phone(query)
            if term is None:
                return {"status": "INVALID_PHONE", "contacts": []}
            q = q.where(cast(Contact.phones, Text).contains(json.dumps(term), autoescape=True))
        elif kind == "email":
            term = query.strip().casefold()
            q = q.where(cast(Contact.emails, Text).contains(json.dumps(term), autoescape=True))
        else:
            raise ValueError("CONTACTS_QUERY_INVALID")
        secret = self.unpack(row)
        incomplete = not secret.get("sync") or bool(secret.get("page"))
        candidates = session.scalars(q.order_by(Contact.resource).limit(limit + 1)).all()
        if kind in {"phone", "email"}:
            candidates = [
                p for p in candidates if term in (p.phones if kind == "phone" else p.emails)
            ]
        return {
            "status": "INCOMPLETE" if incomplete or len(candidates) > limit else "OK",
            "contacts": [
                {
                    "resource": p.resource,
                    "etag": p.etag,
                    "person": p.person,
                    "source": "GOOGLE_CONTACTS",
                    "identity_verified": False,
                }
                for p in candidates[:limit]
            ],
            "last_sync": row.last_sync,
        }

    def lookup(self, session, *, session_token, phone):
        result = self.search(session, session_token=session_token, query=phone, kind="phone")
        matches = result["contacts"]
        if result["status"] in {"INVALID_PHONE", "INCOMPLETE"}:
            return {"status": result["status"]}
        if len(matches) != 1:
            return {"status": "AMBIGUOUS" if matches else "NOT_FOUND"}
        p = matches[0]
        names = p["person"].get("names", [])
        return {
            "status": "MATCH",
            "resource": p["resource"],
            "saved_as": next((n.get("displayName") for n in names if n.get("displayName")), None),
            "source": "GOOGLE_CONTACTS",
            "identity_verified": False,
            "phone": canonical_phone(phone),
            "etag": p["etag"],
        }

    def match_whatsapp(self, session, *, session_token, phone):
        # Exact international number only. Never infer a PN from a LID or a name.
        scope = self._scope(session, session_token)
        match = self.lookup(session, session_token=session_token, phone=phone)
        if match["status"] != "MATCH":
            return {"status": match["status"], "identity_verified": False}
        from attention_router.infrastructure.models import ConversationMessageRow

        number = canonical_phone(phone)
        known = session.scalars(
            select(ConversationMessageRow.external_sender_key)
            .where(
                ConversationMessageRow.tenant_id == scope[0],
                ConversationMessageRow.source == "wwebjs",
                ConversationMessageRow.from_me.is_(False),
                ConversationMessageRow.external_sender_key == number[1:] + "@c.us",
            )
            .distinct()
            .limit(2)
        ).all()
        return {
            "status": "CANDIDATE" if len(known) == 1 else "NOT_FOUND",
            "resource": match["resource"],
            "basis": "EXACT_INTERNATIONAL_PHONE",
            "source": "GOOGLE_CONTACTS_AND_WHATSAPP",
            "identity_verified": False,
            "authorizes_actions": False,
        }

    def sync_one(self, session):
        row = session.scalar(
            select(Connection)
            .where(Connection.status == "ACTIVE", Connection.next_run <= self.now())
            .order_by(Connection.next_run)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return False
        try:
            secret = self.unpack(row)
            grant = self.oauth.refresh(secret["refresh"])
            secret["refresh"] = grant.refresh_token or secret["refresh"]
            data = self.people.page(
                grant.access_token, sync=secret.get("sync"), page=secret.get("page")
            )
            for person in data.get("connections", []):
                resource = person.get("resourceName")
                if (
                    not isinstance(resource, str)
                    or not resource.startswith("people/")
                    or len(resource) > 240
                ):
                    raise ContactsProviderError("INVALID_RESPONSE")
                contact = session.get(Contact, (row.id, resource))
                if contact is None:
                    contact = Contact(
                        connection_id=row.id,
                        resource=resource,
                        person={},
                        search_name="",
                        phones=[],
                        emails=[],
                        generation=row.generation,
                        deleted=False,
                    )
                    session.add(contact)
                contact.etag = person.get("etag")
                contact.generation = row.generation
                contact.deleted = person.get("metadata", {}).get("deleted") is True
                contact.person = {} if contact.deleted else person
                contact.search_name = fold(
                    " ".join(n.get("displayName", "") for n in contact.person.get("names", []))
                )
                contact.phones = sorted(
                    {
                        v
                        for p in contact.person.get("phoneNumbers", [])
                        if (v := canonical_phone(p.get("canonicalForm") or p.get("value")))
                    }
                )
                contact.emails = sorted(
                    {
                        e["value"].strip().casefold()
                        for e in contact.person.get("emailAddresses", [])
                        if isinstance(e.get("value"), str)
                    }
                )
                session.flush()
            page = data.get("nextPageToken")
            if page and page == secret.get("page"):
                raise ContactsProviderError("PAGINATION_LOOP")
            if not page:
                token = data.get("nextSyncToken")
                if not token:
                    raise ContactsProviderError("SYNC_TOKEN_MISSING")
                if not secret.get("sync"):
                    for old in session.scalars(
                        select(Contact).where(
                            Contact.connection_id == row.id, Contact.generation != row.generation
                        )
                    ):
                        old.deleted = True
                        old.person = {}
                        old.phones = []
                        old.emails = []
                        old.search_name = ""
                secret["sync"] = token
                row.last_sync = self.now()
            secret["page"] = page
            self.pack(row, secret)
            row.next_run = self.now() + (30 if page else 900)
            row.failures = 0
            row.error = None
            session.commit()
            return True
        except Exception as exc:
            row_id = row.id
            session.rollback()
            row = session.scalar(
                select(Connection).where(Connection.id == row_id).with_for_update()
            )
            if row.status != "ACTIVE":
                return False
            code = str(exc) if isinstance(exc, ContactsProviderError) else "SYNC_FAILED"
            row.error = code
            row.failures += 1
            row.next_run = self.now() + min(21600, 30 * 2 ** min(row.failures, 10))
            if code == "SYNC_EXPIRED":
                secret = self.unpack(row)
                secret["sync"] = None
                secret["page"] = None
                row.generation = uuid4().hex
                self.pack(row, secret)
                row.next_run = self.now() + 30
            if code == "AUTH_REQUIRED":
                row.status = "AUTH_REQUIRED"
            session.commit()
            return False
