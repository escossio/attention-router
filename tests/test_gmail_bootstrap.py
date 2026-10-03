from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib import parse as urllib_parse

import pytest
from sqlalchemy import select

from attention_router.application.gmail_bootstrap import (
    GmailBootstrapProductService,
    GmailBootstrapReadonlyRequired,
    GmailBootstrapSource,
    GmailBootstrapSourceAuthorityInvalid,
)
from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GMAIL_READONLY_SCOPE,
    GmailConnectionService,
    GoogleGmailProfile,
    GoogleTokenGrant,
)
from attention_router.application.gmail_product_runner import GmailProductRunner
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.client_bootstrap_models import (
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationThreadRow,
    ExecutionIntentRow,
    IntegrationBindingRow,
    OutboxMessageRow,
    RelationshipRow,
    TenantRow,
)
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from attention_router.infrastructure.repository import upsert_actor_binding
from attention_router.config import Settings
from attention_router.integrations.gmail_bootstrap import (
    GmailBootstrapApiReader,
    GmailBootstrapContractError,
    GmailBootstrapHistoryAdapter,
)
from attention_router.integrations.gmail_api_reader import (
    StaticGmailAccessTokenProvider,
)


TENANT = DEFAULT_TENANT_ID
HUMAN = "hid-gmail-bootstrap-owner"
OWNER_ACTOR = "owner-gmail-bootstrap"
SESSION_TOKEN = "cst_" + "g" * 43
ACCOUNT = "owner@example.invalid"


def _key() -> str:
    return base64.urlsafe_b64encode(b"G" * 32).decode().rstrip("=")


class FakeClientSessions:
    def authenticated_bootstrap(self, session, *, session_token, now=None):
        assert session_token == SESSION_TOKEN
        return SimpleNamespace(
            active_tenant_id=TENANT,
            human_identity_id=HUMAN,
        )


class ReadonlyOAuth:
    def __init__(self, scope=GMAIL_READONLY_SCOPE):
        self.scope = scope

    def exchange_authorization_code(self, code):
        assert code == "gmail-bootstrap-code"
        return GoogleTokenGrant(
            access_token="initial-access-token",
            refresh_token="stored-refresh-token",
            granted_scopes=(self.scope,),
        )

    def gmail_profile(self, access_token):
        assert access_token == "initial-access-token"
        return GoogleGmailProfile(ACCOUNT)

    def revoke(self, refresh_token):
        raise AssertionError("revoke not expected")


class FakeRefresher:
    def __init__(self):
        self.seen = []

    def refresh_access_token(self, refresh_token):
        self.seen.append(refresh_token)
        return "transient-access-token"


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "app_env": "test",
        "admin_auth_enabled": False,
        "internal_ingress_hmac_secret": "i" * 32,
        "client_session_enabled": True,
        "gmail_connect_enabled": True,
        "personal_context_bootstrap_enabled": True,
        "gmail_bootstrap_enabled": True,
        "google_workspace_oauth_client_id": "client-id",
        "google_workspace_oauth_client_secret": "client-secret",
        "provider_authorization_key_b64url": _key(),
        "gmail_bootstrap_snapshot_limit": 4,
        "gmail_bootstrap_max_body_bytes": 4096,
        "gmail_bootstrap_max_mime_depth": 6,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_owner(session) -> None:
    stamp = now_utc()
    tenant = session.get(TenantRow, TENANT)
    if tenant is None:
        session.add(
            TenantRow(
                id=TENANT,
                slug="gmail-bootstrap",
                name="Gmail Bootstrap",
                status="ACTIVE",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    if session.get(HumanIdentityRow, HUMAN) is None:
        session.add(HumanIdentityRow(id=HUMAN, created_at=stamp))
    session.add(
        ClientTenantMembershipRow(
            id=new_id(),
            human_identity_id=HUMAN,
            tenant_id=TENANT,
            role="OWNER",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    upsert_actor_binding(
        session,
        source="test",
        external_actor_id="gmail-bootstrap-owner-external",
        actor_key=OWNER_ACTOR,
        actor_category="owner",
        display_name="Owner",
        metadata={"owner": True},
        tenant_id=TENANT,
    )
    session.flush()


def _connect(session, configured, *, scope=GMAIL_READONLY_SCOPE):
    _seed_owner(session)
    service = GmailConnectionService(
        settings=configured,
        client_sessions=FakeClientSessions(),
        oauth=ReadonlyOAuth(scope),
    )
    result = service.connect(
        session,
        session_token=SESSION_TOKEN,
        authorization_code="gmail-bootstrap-code",
        now=datetime(2026, 10, 3, 20, 0, tzinfo=UTC),
    )
    session.flush()
    return result.installation_id


def test_gmail_bootstrap_product_reuses_connected_readonly_authorization(session):
    configured = _settings()
    installation_id = _connect(session, configured)

    service = GmailBootstrapProductService(
        settings=configured,
        client_sessions=FakeClientSessions(),
    )
    row = service.create_and_queue(
        session,
        session_token=SESSION_TOKEN,
        consent_ref="gmail-bootstrap-consent-001",
    )

    authorization = session.get(ProviderAuthorizationRow, installation_id)
    assert row.state == "QUEUED"
    assert row.source_kind == "GMAIL_TEXT"
    assert row.source_account == f"sha256:{authorization.provider_account_hash}"
    assert row.source_selection == {
        "chat_keys": [GmailBootstrapHistoryAdapter.MAILBOX_KEY]
    }
    assert row.processing_budget == {
        "page_size": 4,
        "max_messages_per_chat": 4,
        "max_total_messages": 4,
    }


def test_gmail_bootstrap_rejects_metadata_only_without_scope_escalation(session):
    configured = _settings()
    _connect(session, configured, scope=GMAIL_METADATA_SCOPE)

    service = GmailBootstrapProductService(
        settings=configured,
        client_sessions=FakeClientSessions(),
    )
    with pytest.raises(GmailBootstrapReadonlyRequired):
        service.create_and_queue(
            session,
            session_token=SESSION_TOKEN,
            consent_ref="gmail-bootstrap-consent-metadata",
        )

    assert session.scalar(
        select(PersonalContextBootstrapRunRow).where(
            PersonalContextBootstrapRunRow.source_kind == "GMAIL_TEXT"
        )
    ) is None


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._raw

    def close(self):
        pass


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _gmail_payload(
    message_id: str,
    *,
    thread_id: str,
    sender: str,
    to: str,
    cc: str = "",
    subject: str,
    body: str,
    timestamp_ms: str,
):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    raw = body.encode("utf-8")
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": timestamp_ms,
        "payload": {
            "mimeType": "multipart/alternative",
            "filename": "",
            "headers": headers,
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "filename": "",
                    "headers": [
                        {
                            "name": "Content-Type",
                            "value": "text/plain; charset=utf-8",
                        }
                    ],
                    "body": {
                        "size": len(raw),
                        "data": _b64(body),
                    },
                },
                {
                    "mimeType": "text/html",
                    "filename": "",
                    "headers": [
                        {
                            "name": "Content-Type",
                            "value": "text/html; charset=utf-8",
                        }
                    ],
                    "body": {
                        "size": len(b"<b>ignored</b>"),
                        "data": _b64("<b>ignored</b>"),
                    },
                },
            ],
        },
    }


class FakeGmailApi:
    def __init__(self):
        self.calls = []
        self.message_ids = ["m-new", "m-old"]
        self.messages = {
            "m-new": _gmail_payload(
                "m-new",
                thread_id="thread-new",
                sender=f"Owner <{ACCOUNT}>",
                to="friend@example.invalid",
                subject="Plano novo",
                body="Meu nome é Leonardo.",
                timestamp_ms="1790985600000",
            ),
            "m-old": _gmail_payload(
                "m-old",
                thread_id="thread-old",
                sender="Friend <friend@example.invalid>",
                to=ACCOUNT,
                cc="other@example.invalid",
                subject="Projeto",
                body="Trabalho na Example Corp.",
                timestamp_ms="1790899200000",
            ),
        }

    def __call__(self, request, timeout):
        parsed = urllib_parse.urlparse(request.full_url)
        query = urllib_parse.parse_qs(parsed.query)
        self.calls.append((parsed.path, query, timeout))
        if parsed.path.endswith("/profile"):
            return _Response({"emailAddress": ACCOUNT})
        if parsed.path.endswith("/messages"):
            return _Response(
                {
                    "messages": [
                        {"id": message_id}
                        for message_id in self.message_ids
                    ],
                    "nextPageToken": "ignored-bounded-v0",
                    "resultSizeEstimate": 200,
                }
            )
        message_id = parsed.path.rsplit("/", 1)[-1]
        return _Response(self.messages[message_id])


def _reader(fake: FakeGmailApi) -> GmailBootstrapApiReader:
    return GmailBootstrapApiReader(
        token_provider=StaticGmailAccessTokenProvider(
            "synthetic-access-token"
        ),
        timeout_seconds=2.0,
        opener=fake,
    )


def test_gmail_bootstrap_adapter_freezes_ids_and_resumes_after_restart():
    fake = FakeGmailApi()
    reader = _reader(fake)
    adapter = GmailBootstrapHistoryAdapter(
        reader=reader,
        account_email=reader.profile_email_address(),
        snapshot_limit=2,
        max_body_bytes=4096,
        max_mime_depth=6,
    )

    first = adapter.fetch_messages(
        GmailBootstrapHistoryAdapter.MAILBOX_KEY,
        1,
    )
    assert first["messages"][0]["source_message_id"] == "m-new"
    assert first["messages"][0]["from_me"] is True
    assert "Meu nome é Leonardo." in first["messages"][0]["text"]

    # New provider state cannot reorder the already-frozen cursor.
    fake.message_ids = ["m-surprise", "m-new", "m-old"]
    restarted = GmailBootstrapHistoryAdapter(
        reader=_reader(fake),
        account_email=ACCOUNT,
        snapshot_limit=2,
        max_body_bytes=4096,
        max_mime_depth=6,
    )
    second = restarted.fetch_messages(
        GmailBootstrapHistoryAdapter.MAILBOX_KEY,
        1,
        first["next_cursor"],
    )

    assert second["messages"][0]["source_message_id"] == "m-old"
    assert second["messages"][0]["from_me"] is False
    assert second["messages"][0]["thread_type"] == "GROUP"
    assert second["next_cursor"] is None

    message_list_calls = [
        call for call in fake.calls
        if call[0].endswith("/messages")
    ]
    assert len(message_list_calls) == 1


def test_gmail_bootstrap_archives_real_thread_keys_and_subject_text(session):
    configured = _settings(gmail_bootstrap_snapshot_limit=2)
    _connect(session, configured)

    fake = FakeGmailApi()
    reader = _reader(fake)
    adapter = GmailBootstrapHistoryAdapter(
        reader=reader,
        account_email=ACCOUNT,
        snapshot_limit=2,
        max_body_bytes=4096,
        max_mime_depth=6,
    )

    service = GmailBootstrapProductService(
        settings=configured,
        client_sessions=FakeClientSessions(),
    )
    run = service.create_and_queue(
        session,
        session_token=SESSION_TOKEN,
        consent_ref="gmail-bootstrap-consent-archive",
        processing_budget={
            "page_size": 2,
            "max_messages_per_chat": 2,
            "max_total_messages": 2,
        },
    )

    from attention_router.application.personal_context_bootstrap import (
        process_next_bootstrap_batch,
    )

    result = process_next_bootstrap_batch(
        session,
        run.id,
        adapter=adapter,
    )
    assert result.state == "COMPLETED"

    rows = list(
        session.scalars(
            select(ConversationMessageRow).where(
                ConversationMessageRow.source == "gmail"
            )
        ).all()
    )
    assert len(rows) == 2
    threads = list(
        session.scalars(
            select(ConversationThreadRow).where(
                ConversationThreadRow.source == "gmail"
            )
        ).all()
    )
    assert {thread.external_thread_key for thread in threads} == {
        "gmail:thread-new",
        "gmail:thread-old",
    }
    assert {row.direction for row in rows} == {"INBOUND", "OUTBOUND"}
    assert any("Assunto: Projeto" in (row.text or "") for row in rows)
    assert session.scalar(
        select(ExecutionIntentRow).limit(1)
    ) is None
    assert session.scalar(
        select(OutboxMessageRow).limit(1)
    ) is None
    assert session.scalar(
        select(RelationshipRow).limit(1)
    ) is None


def test_gmail_bootstrap_source_reuses_governed_refresh_authority(session):
    configured = _settings(gmail_bootstrap_snapshot_limit=2)
    installation_id = _connect(session, configured)
    authorization = session.get(ProviderAuthorizationRow, installation_id)

    service = GmailBootstrapProductService(
        settings=configured,
        client_sessions=FakeClientSessions(),
    )
    run = service.create_and_queue(
        session,
        session_token=SESSION_TOKEN,
        consent_ref="gmail-bootstrap-consent-source",
    )

    refresher = FakeRefresher()

    class SourceReader:
        def __init__(self, token):
            self.token = token

        def profile_email_address(self):
            return ACCOUNT

    source = GmailBootstrapSource(
        settings=configured,
        runner=GmailProductRunner(
            settings=configured,
            token_refresher=refresher,
        ),
        reader_factory=lambda token: SourceReader(token),
    )
    adapter = source.adapter_for_run(
        session,
        run,
        now=datetime(2026, 10, 3, 20, 5, tzinfo=UTC),
    )

    assert adapter.account_email == ACCOUNT
    assert adapter.snapshot_limit == 2
    assert refresher.seen == ["stored-refresh-token"]
    assert run.source_account == f"sha256:{authorization.provider_account_hash}"


def test_gmail_bootstrap_reader_reads_externalized_plain_body_with_hard_bound():
    raw_text = "historical externalized body"
    payload = _gmail_payload(
        "m-external",
        thread_id="thread-external",
        sender="Friend <friend@example.invalid>",
        to=ACCOUNT,
        subject="Externalized",
        body=raw_text,
        timestamp_ms="1790812800000",
    )
    plain = payload["payload"]["parts"][0]
    plain["body"] = {
        "size": len(raw_text.encode("utf-8")),
        "attachmentId": "body-part-1",
    }

    class ExternalBodyApi:
        def __init__(self):
            self.attachment_reads = 0

        def __call__(self, request, timeout):
            parsed = urllib_parse.urlparse(request.full_url)
            if parsed.path.endswith("/messages/m-external"):
                return _Response(payload)
            if parsed.path.endswith(
                "/messages/m-external/attachments/body-part-1"
            ):
                self.attachment_reads += 1
                return _Response(
                    {
                        "size": len(raw_text.encode("utf-8")),
                        "data": _b64(raw_text),
                    }
                )
            raise AssertionError(parsed.path)

    fake = ExternalBodyApi()
    reader = GmailBootstrapApiReader(
        token_provider=StaticGmailAccessTokenProvider(
            "synthetic-access-token"
        ),
        timeout_seconds=2.0,
        opener=fake,
    )
    message = reader.read_bootstrap_message(
        "m-external",
        max_body_bytes=4096,
        max_mime_depth=6,
    )
    assert message.body == raw_text
    assert fake.attachment_reads == 1

    with pytest.raises(GmailBootstrapContractError):
        reader.read_bootstrap_message(
            "m-external",
            max_body_bytes=4,
            max_mime_depth=6,
        )


def test_gmail_bootstrap_reader_quotes_provider_message_id():
    class QuotedIdApi(FakeGmailApi):
        def __init__(self):
            super().__init__()
            self.message_ids = ["m/with?reserved"]
            self.messages = {
                "m/with?reserved": _gmail_payload(
                    "m/with?reserved",
                    thread_id="thread-quoted",
                    sender="Friend <friend@example.invalid>",
                    to=ACCOUNT,
                    subject="Quoted",
                    body="Body.",
                    timestamp_ms="1790899200000",
                )
            }

        def __call__(self, request, timeout):
            parsed = urllib_parse.urlparse(request.full_url)
            query = urllib_parse.parse_qs(parsed.query)
            self.calls.append((parsed.path, query, timeout))
            if parsed.path.endswith("/profile"):
                return _Response({"emailAddress": ACCOUNT})
            if parsed.path.endswith("/messages"):
                return _Response({"messages": [{"id": self.message_ids[0]}]})
            assert parsed.path.endswith("/messages/m%2Fwith%3Freserved")
            return _Response(self.messages["m/with?reserved"])

    fake = QuotedIdApi()
    reader = _reader(fake)
    message = reader.read_bootstrap_message(
        "m/with?reserved",
        max_body_bytes=4096,
        max_mime_depth=6,
    )

    assert message.message_id == "m/with?reserved"
    assert message.body == "Body."


def test_gmail_bootstrap_adapter_rejects_invalid_constructor_bounds():
    fake = FakeGmailApi()
    reader = _reader(fake)

    with pytest.raises(GmailBootstrapContractError):
        GmailBootstrapHistoryAdapter(
            reader=reader,
            account_email=ACCOUNT,
            snapshot_limit=2,
            max_body_bytes=0,
            max_mime_depth=6,
        )
    with pytest.raises(GmailBootstrapContractError):
        GmailBootstrapHistoryAdapter(
            reader=reader,
            account_email=ACCOUNT,
            snapshot_limit=2,
            max_body_bytes=4096,
            max_mime_depth=0,
        )


def test_gmail_bootstrap_run_is_pinned_to_original_account_fingerprint(session):
    configured = _settings(gmail_bootstrap_snapshot_limit=2)
    installation_id = _connect(session, configured)

    service = GmailBootstrapProductService(
        settings=configured,
        client_sessions=FakeClientSessions(),
    )
    run = service.create_and_queue(
        session,
        session_token=SESSION_TOKEN,
        consent_ref="gmail-bootstrap-consent-account-pin",
    )

    authorization = session.get(ProviderAuthorizationRow, installation_id)
    binding = session.get(
        IntegrationBindingRow,
        authorization.integration_binding_id,
    )
    replacement_hash = hashlib.sha256(
        b"replacement@example.invalid"
    ).hexdigest()
    authorization.provider_account_hash = replacement_hash
    binding.account_key = f"sha256:{replacement_hash}"
    session.flush()

    source = GmailBootstrapSource(
        settings=configured,
        runner=GmailProductRunner(
            settings=configured,
            token_refresher=FakeRefresher(),
        ),
        reader_factory=lambda token: pytest.fail(
            "reader must not be created after account replacement"
        ),
    )
    with pytest.raises(GmailBootstrapSourceAuthorityInvalid):
        source.adapter_for_run(
            session,
            run,
            now=datetime(2026, 10, 3, 20, 10, tzinfo=UTC),
        )
