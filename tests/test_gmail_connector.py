from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import stat

import httpx
import pytest

from attention_router.connectors.gmail import (
    FileGmailCursorStore,
    GmailApiClient,
    GmailBacklogLimitExceeded,
    GmailConnectorConfig,
    GmailConnectorError,
    GmailCursorState,
    GmailHistoryExpired,
    GmailPollingConnector,
)
from attention_router.integrations.http_client import (
    IntegrationIngressClientError,
    NeutralIntegrationIngressClient,
)


@dataclass
class MemoryCursorStore:
    state: GmailCursorState | None = None

    def load(self) -> GmailCursorState | None:
        return self.state

    def save(self, state: GmailCursorState) -> None:
        self.state = state


def _config(*, message_limit: int = 100) -> GmailConnectorConfig:
    return GmailConnectorConfig(
        tenant_id="tenant-1",
        instance_id="gmail-primary",
        account_id="gmail-account-opaque",
        integration_endpoint=(
            "https://router.example/api/v1/ingress/integrations/events"
        ),
        integration_credential="integration-secret",
        cursor_path=Path("/tmp/not-used"),
        message_limit_per_poll=message_limit,
        history_page_limit=5,
        request_timeout_seconds=5,
    )


def _mailbox_hash(address: str) -> str:
    return hashlib.sha256(address.casefold().encode()).hexdigest()


def _gmail_client(handler) -> GmailApiClient:
    return GmailApiClient(
        access_token_provider=lambda: "gmail-access-token",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )


def _ingress_client(handler) -> NeutralIntegrationIngressClient:
    return NeutralIntegrationIngressClient(
        endpoint="https://router.example/api/v1/ingress/integrations/events",
        credential="integration-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )


def _profile(history_id: str = "105", email: str = "owner@example.invalid"):
    return {"emailAddress": email, "historyId": history_id}


def _history(*message_ids: str, history_id: str = "105"):
    return {
        "historyId": history_id,
        "history": [
            {
                "id": str(101 + index),
                "messagesAdded": [
                    {"message": {"id": message_id, "threadId": f"t-{message_id}"}}
                ],
            }
            for index, message_id in enumerate(message_ids)
        ],
    }


def _message(message_id: str = "m1"):
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "internalDate": "1789912800000",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {
                    "name": "From",
                    "value": "Synthetic Sender <sender@example.invalid>",
                }
            ]
        },
    }


def test_config_reads_integration_credential_from_private_file(
    tmp_path,
    monkeypatch,
):
    secret = tmp_path / "integration-bearer"
    secret.write_text("integration-secret-from-file\n", encoding="utf-8")
    secret.chmod(0o600)

    values = {
        "GMAIL_TENANT_ID": "tenant-1",
        "GMAIL_INTEGRATION_INSTANCE_ID": "gmail-primary",
        "GMAIL_INTEGRATION_ACCOUNT_ID": "gmail-account-opaque",
        "GMAIL_CONNECTOR_STATE_PATH": str(tmp_path / "cursor.json"),
        "ATTENTION_ROUTER_INTEGRATION_ENDPOINT": (
            "https://router.example/api/v1/ingress/integrations/events"
        ),
        "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE": str(secret),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(
        "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL",
        raising=False,
    )

    config = GmailConnectorConfig.from_env()

    assert config.integration_credential == "integration-secret-from-file"
    assert config.cursor_path == tmp_path / "cursor.json"


def test_config_rejects_group_readable_integration_credential_file(
    tmp_path,
    monkeypatch,
):
    credential_file = tmp_path / "integration-bearer"
    credential_file.write_text("integration-secret", encoding="utf-8")
    credential_file.chmod(0o644)
    values = {
        "GMAIL_TENANT_ID": "tenant-1",
        "GMAIL_INTEGRATION_INSTANCE_ID": "gmail-primary",
        "GMAIL_INTEGRATION_ACCOUNT_ID": "gmail-account-opaque",
        "GMAIL_CONNECTOR_STATE_PATH": str(tmp_path / "cursor.json"),
        "ATTENTION_ROUTER_INTEGRATION_ENDPOINT": (
            "https://router.example/api/v1/ingress/integrations/events"
        ),
        "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE": str(credential_file),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(
        "ATTENTION_ROUTER_INTEGRATION_CREDENTIAL",
        raising=False,
    )

    with pytest.raises(
        GmailConnectorError,
        match="ATTENTION_ROUTER_INTEGRATION_CREDENTIAL_FILE_PERMISSIONS_UNSAFE",
    ):
        GmailConnectorConfig.from_env()


def test_first_poll_bootstraps_current_history_without_ingesting_old_mail():
    calls = []

    def gmail_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith("/profile")
        return httpx.Response(200, json=_profile(history_id="100"))

    def ingress_handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("bootstrap must not send historical mail")

    store = MemoryCursorStore()
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(ingress_handler),
        cursor_store=store,
    )

    result = connector.poll_once()

    assert result.status == "BOOTSTRAPPED"
    assert result.messages_seen == 0
    assert result.history_id == "100"
    assert store.state is not None
    assert store.state.history_id == "100"
    assert calls == ["/gmail/v1/users/me/profile"]


def test_incremental_history_normalizes_metadata_and_advances_cursor_after_ack():
    gmail_calls = []
    ingress_bodies = []

    def gmail_handler(request: httpx.Request) -> httpx.Response:
        gmail_calls.append(str(request.url))
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=_profile())
        if request.url.path.endswith("/history"):
            assert request.url.params["startHistoryId"] == "100"
            assert request.url.params["historyTypes"] == "messageAdded"
            assert request.url.params["labelId"] == "INBOX"
            return httpx.Response(
                200,
                json={
                    "historyId": "105",
                    "history": [
                        {
                            "id": "101",
                            "messagesAdded": [
                                {"message": {"id": "m1", "threadId": "t-m1"}},
                                {"message": {"id": "m1", "threadId": "t-m1"}},
                            ],
                        }
                    ],
                },
            )
        if request.url.path.endswith("/messages/m1"):
            assert request.url.params["format"] == "metadata"
            assert request.url.params["metadataHeaders"] == "From"
            return httpx.Response(200, json=_message())
        raise AssertionError(request.url)

    def ingress_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ingress_bodies.append(request.content)
        assert payload["tenant_id"] == "tenant-1"
        assert payload["source"] == {
            "kind": "CHANNEL",
            "name": "channel.email",
            "instance_id": "gmail-primary",
            "account_id": "gmail-account-opaque",
        }
        assert payload["external_event_id"] == "m1"
        assert payload["actor"]["external_actor_id"] == "sender@example.invalid"
        assert payload["actor"]["display_name"] == "Synthetic Sender"
        assert payload["thread"]["external_thread_id"] == "t-m1"
        assert payload["payload_type"] == "EMAIL_MESSAGE_REFERENCE"
        assert payload["payload_ref"] == {"message_ref": "gmail:m1"}
        assert payload["artifact_ids"] == []
        assert payload["metadata_sanitized"] == {
            "subject_present": False,
            "body_present": False,
            "attachment_count": 0,
            "recipient_count": 0,
        }
        return httpx.Response(
            202,
            json={
                "transport_version": "1",
                "status": "accepted",
                "receipt_id": "receipt-m1",
                "admitted_at": "2026-09-20T20:00:01Z",
                "correlation_id": payload["correlation_id"],
            },
        )

    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(ingress_handler),
        cursor_store=store,
    )

    result = connector.poll_once()

    assert result.status == "SYNCED"
    assert result.messages_seen == 1
    assert result.accepted == 1
    assert result.duplicates == 0
    assert result.history_id == "105"
    assert store.state is not None
    assert store.state.history_id == "105"
    assert len(ingress_bodies) == 1
    assert len([call for call in gmail_calls if "/messages/m1" in call]) == 1


def test_repoll_of_same_history_produces_byte_identical_duplicate_request():
    request_bodies = []
    ingress_status = iter([(202, "accepted"), (200, "duplicate")])

    def gmail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=_profile())
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json=_history("m1"))
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json=_message())
        raise AssertionError(request.url)

    def ingress_handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content)
        status_code, status = next(ingress_status)
        payload = json.loads(request.content)
        return httpx.Response(
            status_code,
            json={
                "transport_version": "1",
                "status": status,
                "receipt_id": "same-receipt",
                "admitted_at": "2026-09-20T20:00:01Z",
                "correlation_id": payload["correlation_id"],
            },
        )

    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(ingress_handler),
        cursor_store=store,
    )

    first = connector.poll_once()
    store.state = GmailCursorState(
        mailbox_sha256=_mailbox_hash("owner@example.invalid"),
        history_id="100",
    )
    second = connector.poll_once()

    assert first.accepted == 1
    assert second.duplicates == 1
    assert request_bodies[0] == request_bodies[1]


def test_ingress_failure_never_advances_gmail_cursor():
    def gmail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=_profile())
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json=_history("m1"))
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json=_message())
        raise AssertionError(request.url)

    ingress = _ingress_client(
        lambda _request: httpx.Response(
            503,
            json={
                "transport_version": "1",
                "error_code": "INGRESS_UNAVAILABLE",
                "request_id": "req-1",
            },
        )
    )
    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=ingress,
        cursor_store=store,
    )

    with pytest.raises(IntegrationIngressClientError):
        connector.poll_once()

    assert store.state is not None
    assert store.state.history_id == "100"


def test_expired_history_cursor_fails_closed_without_resetting_state():
    def gmail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=_profile(history_id="999"))
        if request.url.path.endswith("/history"):
            return httpx.Response(404, json={"error": {"code": 404}})
        raise AssertionError(request.url)

    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(
            lambda _request: pytest.fail("stale cursor must not ingest")
        ),
        cursor_store=store,
    )

    with pytest.raises(
        GmailHistoryExpired,
        match="GMAIL_HISTORY_CURSOR_EXPIRED",
    ):
        connector.poll_once()

    assert store.state is not None
    assert store.state.history_id == "100"


def test_mailbox_identity_change_fails_closed_before_history_or_ingress():
    calls = []

    def gmail_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=_profile(email="different@example.invalid"),
        )

    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(
            lambda _request: pytest.fail("identity change must not ingest")
        ),
        cursor_store=store,
    )

    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_MAILBOX_IDENTITY_CHANGED",
    ):
        connector.poll_once()

    assert calls == ["/gmail/v1/users/me/profile"]
    assert store.state.history_id == "100"


def test_backlog_limit_is_checked_before_any_message_is_sent():
    message_gets = []
    ingress_calls = []

    def gmail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=_profile())
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json=_history("m1", "m2"))
        if "/messages/" in request.url.path:
            message_gets.append(request.url.path)
            return httpx.Response(200, json=_message())
        raise AssertionError(request.url)

    def ingress_handler(request: httpx.Request) -> httpx.Response:
        ingress_calls.append(request)
        raise AssertionError("backlog overflow must fail before ingress")

    store = MemoryCursorStore(
        GmailCursorState(
            mailbox_sha256=_mailbox_hash("owner@example.invalid"),
            history_id="100",
        )
    )
    connector = GmailPollingConnector(
        config=_config(message_limit=1),
        gmail=_gmail_client(gmail_handler),
        ingress=_ingress_client(ingress_handler),
        cursor_store=store,
    )

    with pytest.raises(
        GmailBacklogLimitExceeded,
        match="GMAIL_MESSAGE_BACKLOG_LIMIT_EXCEEDED",
    ):
        connector.poll_once()

    assert message_gets == []
    assert ingress_calls == []
    assert store.state.history_id == "100"


def test_file_cursor_store_is_atomic_private_and_rejects_unsafe_permissions(
    tmp_path,
):
    path = tmp_path / "gmail-cursor.json"
    store = FileGmailCursorStore(path)
    state = GmailCursorState(
        mailbox_sha256=_mailbox_hash("owner@example.invalid"),
        history_id="123",
    )

    store.save(state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load() == state

    path.chmod(0o644)
    with pytest.raises(
        GmailConnectorError,
        match="GMAIL_CURSOR_STATE_PERMISSIONS_UNSAFE",
    ):
        store.load()
