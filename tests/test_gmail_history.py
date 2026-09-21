"""Synthetic incremental runner contracts, including replay and bounds."""
from datetime import UTC, datetime
import json
import traceback
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from attention_router.application.gmail_connection import (
    GMAIL_READONLY_SCOPE,
    GmailConnectionService,
    GoogleGmailProfile,
)
from attention_router.application.gmail_history import (
    GmailProductHistoryRecordTooLarge, GmailProductHistoryStale,
)
from attention_router.application.gmail_product_runner import (
    GmailProductRunner, GmailProductRunnerError,
)
from attention_router.config import settings
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.gmail_api_reader import GmailApiReader, StaticGmailAccessTokenProvider
from attention_router.integrations.gmail_connector import (
    GmailAttachmentSummary,
    GmailConnectorError,
    GmailMessage,
    IntegrationIngressResponse,
)
from test_gmail_product_runner import (
    _enabled, _seed_connected, FakeClientSessions, FakeIngress, FakeOAuth,
    FakeReader, FakeRefresher, SESSION_TOKEN,
)

NOW = datetime(2026, 9, 21, 17, tzinfo=UTC)


def record(n, *ids, labels=None):
    return {"id": str(n), "messagesAdded": [
        {"message": {"id": mid, "labelIds": labels if labels is not None else ["INBOX"]}}
        for mid in ids
    ]}


class Reader(FakeReader):
    def __init__(self):
        super().__init__("synthetic-token")
        self.pages = [{"historyId": "20", "history": [record(12, "one")]}]
        self.calls = []
        self.seeds = 0
        self.failure = None

    def current_history_id(self):
        self.seeds += 1
        return "10"

    def history_page(self, start, page_token=None):
        self.calls.append((start, page_token))
        if self.failure:
            raise self.failure
        return self.pages[len(self.calls) - 1]


@pytest.fixture
def harness(session, monkeypatch):
    installation = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    reader = Reader()
    ingress = FakeIngress("synthetic-bearer")
    runner = GmailProductRunner(
        settings=settings, token_refresher=FakeRefresher(),
        reader_factory=lambda token: reader, ingress_factory=lambda bearer: ingress,
    )
    row = session.get(ProviderAuthorizationRow, installation)
    def run(**kwargs):
        return runner.run_incremental(session, installation_id=installation, now=NOW, **kwargs)
    return run, row, reader, ingress


def seed(harness, session):
    run, row, reader, ingress = harness
    result = run()
    assert result.initialized and result.selected == 0 and result.cursor_advanced
    assert not ingress.payloads and not reader.calls and not reader.reads
    assert row.gmail_history_id == "10"
    session.commit()
    return harness


def test_seed_then_metadata_ingress(harness, session):
    run, row, reader, ingress = seed(harness, session)
    result = run()
    assert result.accepted == result.selected == result.history_records_examined == 1
    assert row.gmail_history_id == "20"
    assert reader.calls == [("10", None)]
    metadata = ingress.payloads[0]["metadata_sanitized"]
    assert metadata["body_observed"] is metadata["attachments_observed"] is False
    assert "one" not in repr(result) and "history_id" not in repr(result)
    session.rollback()
    assert row.gmail_history_id == "10"  # method never commits


def test_chronological_dedup_and_irrelevant_records(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [{"historyId": "40", "history": [
        record(11, "a"), record(12, "a", "b"), record(13, "other", labels=["SENT"]),
        {"id": "14", "messages": [{"id": "ignored"}], "labelsAdded": []},
    ]}]
    result = run()
    assert reader.reads == ["a", "b"]
    assert result.history_records_examined == 4 and result.selected == 2
    assert row.gmail_history_id == "40"


def test_duplicate_advances(harness, session):
    run, row, reader, ingress = seed(harness, session)
    ingress.send = lambda payload: IntegrationIngressResponse(200, {"status": "duplicate"})
    assert run().duplicates == 1
    assert row.gmail_history_id == "20"


@pytest.mark.parametrize("failure", [RuntimeError("secret-url-token"),
                                      GmailConnectorError("GMAIL_PRODUCT_HISTORY_STALE")])
def test_provider_failure_stale_no_reseed(harness, session, failure, caplog):
    run, row, reader, ingress = seed(harness, session)
    reader.failure = failure
    with pytest.raises(GmailProductRunnerError) as caught:
        run()
    if isinstance(failure, GmailConnectorError):
        assert isinstance(caught.value, GmailProductHistoryStale)
    assert row.gmail_history_id == "10" and reader.seeds == 1
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert "secret-url-token" not in ''.join(traceback.format_exception(caught.value)) + caplog.text


def test_ingress_failure_retry_is_byte_identical(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [{"historyId": "30", "history": [record(11, "a"), record(12, "b")]}]
    sent = []
    def fail(payload):
        sent.append(json.dumps(payload))
        if len(sent) == 2:
            raise RuntimeError("secret-url-token")
        return IntegrationIngressResponse(202, {"status": "accepted"})
    ingress.send = fail
    with pytest.raises(GmailProductRunnerError, match="GMAIL_PRODUCT_INGRESS_FAILED"):
        run()
    assert row.gmail_history_id == "10"
    reader.calls.clear()
    def retry(payload):
        assert json.dumps(payload) == sent[len(ingress.payloads)]
        ingress.payloads.append(payload)
        return IntegrationIngressResponse(200, {"status": "duplicate"})
    ingress.send = retry
    assert run().duplicates == 2
    assert row.gmail_history_id == "30"


def test_limit_stops_before_whole_record_then_progresses(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [{"historyId": "30", "history": [record(11, "a"), record(12, "b", "c")]}]
    assert run(max_results=2).selected == 1
    assert row.gmail_history_id == "11"
    session.commit()
    reader.calls.clear()
    reader.pages[0]["history"] = [record(12, "b", "c")]
    assert run(max_results=2).selected == 2
    assert row.gmail_history_id == "30" and reader.reads == ["a", "b", "c"]


def test_oversized_record_fails_closed(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages[0]["history"] = [record(12, "a", "b", "c")]
    with pytest.raises(GmailProductHistoryRecordTooLarge):
        run(max_results=2)
    assert row.gmail_history_id == "10" and not reader.reads


def test_pagination_bound_and_empty_progress(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [
        {"historyId": "99", "history": [record(11)], "nextPageToken": "page2"},
        {"historyId": "99", "history": [record(12)], "nextPageToken": "page3"},
    ]
    result = run(max_pages=2)
    assert result.selected == 0 and result.history_records_examined == 2
    assert row.gmail_history_id == "12" and len(reader.calls) == 2


@pytest.mark.parametrize("page", [
    {"historyId": "20", "history": [record(12), record(11)]},
    {"historyId": "20", "history": [record(12)], "nextPageToken": ""},
    {"historyId": "9"}, {"historyId": "20", "history": [record(21)]},
])
def test_invalid_history_never_advances(harness, session, page):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [page]
    with pytest.raises(GmailProductRunnerError):
        run()
    assert row.gmail_history_id == "10"


@pytest.mark.parametrize("field,value", [("status", "REVOKED"), ("granted_scopes", ["wide"]),
                                           ("integration_binding_id", "missing")])
def test_governance_before_io(harness, session, field, value):
    run, row, reader, ingress = harness
    setattr(row, field, value)
    if field == "status":
        row.revoked_at = NOW
    session.commit()
    with pytest.raises(GmailProductRunnerError):
        run()
    assert not reader.calls and reader.seeds == 0


def test_reconnect_same_account_reuses_and_replacement_clears(harness, session):
    run, row, reader, ingress = seed(harness, session)
    oauth = FakeOAuth()
    service = GmailConnectionService(settings=settings, client_sessions=FakeClientSessions(), oauth=oauth)
    def connect():
        service.connect(session, session_token=SESSION_TOKEN, authorization_code="server-auth-code", now=NOW)
        session.commit()
    connect()
    assert row.gmail_history_id == "10"
    oauth.gmail_profile = lambda token: GoogleGmailProfile("replacement@example.invalid")
    connect()
    assert row.gmail_history_id is None
    assert run().initialized
    session.commit()
    oauth.gmail_profile = lambda token: GoogleGmailProfile("owner@example.invalid")
    connect()
    assert row.gmail_history_id is None  # A -> B -> A must not reuse A's old cursor


class Response:
    status = 200
    def __init__(self, raw):
        self.raw = raw
        self.closed = False
    def read(self, limit):
        assert limit == 1024 * 1024 + 1
        return self.raw[:limit]
    def close(self):
        self.closed = True


@pytest.mark.parametrize("raw", [b'{secret-url-token', b'x' * (1024 * 1024 + 2), b'[]'])
def test_reader_response_bounds_sanitized(raw, caplog):
    response = Response(raw)
    reader = GmailApiReader(token_provider=StaticGmailAccessTokenProvider("secret-token"),
                           opener=lambda *a, **k: response)
    with pytest.raises(GmailConnectorError) as caught:
        reader.history_page("10")
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert "secret" not in ''.join(traceback.format_exception(caught.value)) + caplog.text
    assert response.closed


def test_real_reader_profile_history_metadata_only(harness, session):
    run, row, reader, ingress = seed(harness, session)
    calls = []
    from test_gmail_api_reader import _full_message
    def opener(request, **kwargs):
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        calls.append(parsed.path)
        assert request.method == "GET"
        if parsed.path.endswith("/profile"):
            assert query == {"fields": ["historyId"]}
            payload = {"historyId": "10"}
        elif parsed.path.endswith("/history"):
            assert query["historyTypes"] == ["messageAdded"]
            assert query["labelId"] == ["INBOX"] and query["startHistoryId"] == ["10"]
            assert query["maxResults"] == ["100"] and "q" not in query
            payload = {"historyId": "20", "history": [record(12, "gmail-message-1")]}
        else:
            assert query["format"] == ["metadata"]
            payload = _full_message(attachment=True)
        return Response(json.dumps(payload).encode())
    api = GmailApiReader(token_provider=StaticGmailAccessTokenProvider("synthetic-access"), opener=opener)
    assert api.current_history_id() == "10"
    reader.history_page = api.history_page
    reader.read_message = api.read_message
    assert run().accepted == 1
    assert len(calls) == 3


def test_empty_final_page_advances_mailbox_watermark(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [{"historyId": "30"}]
    result = run()
    assert result.cursor_advanced and result.selected == result.history_records_examined == 0
    assert row.gmail_history_id == "30"


def test_repeated_page_token_fails_closed(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages = [
        {"historyId": "30", "history": [record(11)], "nextPageToken": "same"},
        {"historyId": "30", "history": [record(12)], "nextPageToken": "same"},
    ]
    with pytest.raises(GmailProductRunnerError):
        run()
    assert row.gmail_history_id == "10" and len(reader.calls) == 2


def test_second_page_provider_failure_rolls_back_entire_cursor(harness, session):
    run, row, reader, ingress = seed(harness, session)
    reader.pages[0]["nextPageToken"] = "next"
    with pytest.raises(GmailProductRunnerError):  # no second fake page
        run()
    assert row.gmail_history_id == "10" and len(ingress.payloads) == 1


@pytest.mark.parametrize("mode", ["response", "exception"])
def test_real_http_404_history_stale(harness, session, mode):
    from io import BytesIO
    from urllib.error import HTTPError
    run, row, reader, ingress = seed(harness, session)
    def opener(request, **kwargs):
        if mode == "exception":
            raise HTTPError("secret-url-token", 404, "secret-body", {}, BytesIO(b'secret'))
        response = Response(b'secret-body')
        response.status = 404
        return response
    api = GmailApiReader(token_provider=StaticGmailAccessTokenProvider("secret-token"), opener=opener)
    reader.history_page = api.history_page
    with pytest.raises(GmailProductHistoryStale) as caught:
        run()
    assert row.gmail_history_id == "10" and reader.seeds == 1
    assert "secret-" not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("kwargs", [{"max_pages": 0}, {"max_pages": 11},
                                     {"max_pages": True}, {"max_results": 0},
                                     {"max_results": 101}, {"max_results": True}])
def test_invalid_limits_no_io(harness, kwargs):
    run, row, reader, ingress = harness
    with pytest.raises(ValueError):
        run(**kwargs)
    assert not reader.calls and reader.seeds == 0


@pytest.mark.parametrize("mutation", ["credential_revoked", "binding_inactive", "credential_scope",
                                       "ciphertext", "digest", "scope", "disabled"])
def test_incremental_authority_checks(harness, session, monkeypatch, mutation):
    from attention_router.infrastructure.models import IntegrationBindingRow, IntegrationCredentialRow
    run, row, reader, ingress = harness
    credential = session.get(IntegrationCredentialRow, row.integration_credential_id)
    binding = session.get(IntegrationBindingRow, row.integration_binding_id)
    if mutation == "credential_revoked":
        credential.revoked = True
    elif mutation == "binding_inactive":
        binding.active = False
    elif mutation == "credential_scope":
        credential.scopes = ["extra"]
    elif mutation == "ciphertext":
        row.secret_ciphertext_b64url = "invalid"
    elif mutation == "digest":
        credential.digest = "0" * 64
    elif mutation == "scope":
        row.granted_scopes = ["https://www.googleapis.com/auth/gmail.metadata", "extra"]
    else:
        monkeypatch.setattr(settings, "gmail_product_runner_enabled", False)
    session.commit()
    with pytest.raises(GmailProductRunnerError):
        run()
    assert not reader.calls and not reader.seeds and not ingress.payloads


class ReadonlyHistoryReader(Reader):
    def __init__(self):
        super().__init__()
        self.payload = b"history attachment"

    def read_message_with_attachments(
        self,
        message_id,
        *,
        max_attachments,
        max_mime_depth,
    ):
        assert max_attachments >= 1
        assert max_mime_depth >= 1
        return GmailMessage(
            message_id=message_id,
            thread_id="gmail-thread-1",
            sender="Sender <sender@example.invalid>",
            to=("owner@example.invalid",),
            cc=(),
            bcc=(),
            subject="History attachment",
            body="",
            email_ts=NOW.isoformat(),
            attachments=(
                GmailAttachmentSummary(
                    attachment_id="history-att-1",
                    filename="history.pdf",
                    mime_type="application/pdf",
                    size_bytes=len(self.payload),
                ),
            ),
            body_observed=False,
            attachments_observed=True,
        )

    def read_attachment(
        self,
        message_id,
        attachment_id,
        *,
        expected_size,
        max_bytes,
    ):
        assert message_id
        assert attachment_id == "history-att-1"
        assert expected_size == len(self.payload)
        assert max_bytes >= expected_size
        return self.payload


def test_incremental_readonly_artifact_survives_cursor_rollback(
    session,
    monkeypatch,
    tmp_path,
):
    installation = _seed_connected(session, monkeypatch)
    _enabled(monkeypatch)
    monkeypatch.setattr(settings, "artifact_store_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 4096)
    monkeypatch.setattr(settings, "gmail_attachment_ingestion_enabled", True)
    monkeypatch.setattr(settings, "gmail_attachment_max_count", 4)
    monkeypatch.setattr(settings, "gmail_attachment_max_bytes", 1024)
    monkeypatch.setattr(settings, "gmail_attachment_max_total_bytes", 2048)
    monkeypatch.setattr(settings, "gmail_attachment_max_mime_depth", 4)

    row = session.get(ProviderAuthorizationRow, installation)
    row.granted_scopes = [GMAIL_READONLY_SCOPE]
    session.commit()

    reader = ReadonlyHistoryReader()
    ingress = FakeIngress("synthetic-bearer")
    artifact_sessions = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
        future=True,
    )
    runner = GmailProductRunner(
        settings=settings,
        token_refresher=FakeRefresher(),
        reader_factory=lambda token: reader,
        ingress_factory=lambda bearer: ingress,
        artifact_session_factory=artifact_sessions,
    )

    initialized = runner.run_incremental(
        session,
        installation_id=installation,
        now=NOW,
    )
    assert initialized.initialized
    session.commit()
    assert row.gmail_history_id == "10"

    result = runner.run_incremental(
        session,
        installation_id=installation,
        now=NOW,
    )
    assert result.accepted == 1
    assert row.gmail_history_id == "20"
    artifact_id = ingress.payloads[0]["artifact_ids"][0]

    session.rollback()
    session.refresh(row)
    assert row.gmail_history_id == "10"
    artifact = session.get(ArtifactRow, artifact_id)
    receipt = session.scalar(
        select(ArtifactReceiptRow).where(
            ArtifactReceiptRow.artifact_id == artifact_id
        )
    )
    assert artifact is not None
    assert receipt is not None
