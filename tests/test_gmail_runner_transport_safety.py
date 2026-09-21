from __future__ import annotations

from email.message import Message
from io import BytesIO
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.response import addinfourl

import pytest

from attention_router.integrations.gmail_api_reader import (
    GmailApiReader,
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_connector import (
    GmailConnectorConfig,
    GmailConnectorError,
    GmailInboundConnector,
    GmailMessage,
    IntegrationIngressClient,
    IntegrationIngressResponse,
)
from attention_router.integrations.http_transport import urlopen_without_redirects


ACCESS_SENTINEL = "SYNTHETIC_ACCESS_TOKEN_REPR_SENTINEL"
BEARER_SENTINEL = "SYNTHETIC_INGRESS_BEARER_REPR_SENTINEL_0123456789"


def _config():
    return GmailConnectorConfig(
        tenant_id="gmail-transport-synthetic",
        instance_id="gmail-synthetic",
        account_id="sha256:" + "a" * 64,
        ingress_url="https://ingress.example.invalid/api/v1/ingress/integrations/events",
        ingress_bearer=BEARER_SENTINEL,
    )


def _redirect_transport(monkeypatch, status):
    requests = []

    class SyntheticHTTPHandler(urllib_request.HTTPHandler, urllib_request.HTTPSHandler):
        def http_open(self, request):
            requests.append(request)
            if len(requests) > 1:
                pytest.fail("credential-bearing redirect must not reach another endpoint")
            headers = Message()
            headers["Location"] = "https://redirect.example.invalid/collect"
            response = addinfourl(BytesIO(b"{}"), headers, request.full_url, status)
            response.msg = "synthetic redirect"
            return response

        https_open = http_open

    original_build_opener = urllib_request.build_opener

    def build_opener(*handlers):
        # Exercise urllib's real redirect/error processing without any socket.
        return original_build_opener(SyntheticHTTPHandler(), *handlers)

    monkeypatch.setattr(urllib_request, "build_opener", build_opener)
    return requests


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_authenticated_http_transport_refuses_redirects(monkeypatch, status, method):
    requests = _redirect_transport(monkeypatch, status)
    request = urllib_request.Request(
        "https://provider.example.invalid/token",
        data=b"refresh_token=SYNTHETIC_REFRESH_TOKEN" if method == "POST" else None,
        method=method,
        headers={"Authorization": "Bearer " + ACCESS_SENTINEL},
    )

    with pytest.raises(urllib_error.HTTPError) as caught:
        urlopen_without_redirects(request, timeout=1.0)

    assert caught.value.code == status
    assert len(requests) == 1
    assert requests[0].full_url == request.full_url


@pytest.mark.parametrize("client", ["gmail", "ingress"])
def test_gmail_and_ingress_default_transport_refuses_redirects(monkeypatch, client):
    requests = _redirect_transport(monkeypatch, 302)
    if client == "gmail":
        reader = GmailApiReader(token_provider=StaticGmailAccessTokenProvider(ACCESS_SENTINEL))
        with pytest.raises(GmailConnectorError):
            reader.search_message_ids(query="", max_results=1)
    else:
        ingress = IntegrationIngressClient(url=_config().ingress_url, bearer=BEARER_SENTINEL)
        with pytest.raises(GmailConnectorError):
            ingress.send({"synthetic": True})

    assert len(requests) == 1


def test_gmail_credential_containers_hide_secrets_from_repr():
    token = StaticGmailAccessTokenProvider(ACCESS_SENTINEL)
    config = _config()

    for rendered in (repr(token), str(token), repr(config), str(config)):
        assert ACCESS_SENTINEL not in rendered
        assert BEARER_SENTINEL not in rendered

    assert token.access_token() == ACCESS_SENTINEL
    assert config.ingress_bearer == BEARER_SENTINEL


class ExcessReader:
    def __init__(self):
        self.reads = []

    def search_message_ids(self, *, query, max_results):
        assert query == ""
        return tuple(f"synthetic-{index}" for index in range(101))

    def read_message(self, message_id):
        self.reads.append(message_id)
        return GmailMessage(
            message_id=message_id,
            thread_id="synthetic-thread",
            sender="sender@example.invalid",
            to=("owner@example.invalid",),
            cc=(),
            bcc=(),
            subject="",
            body="",
            email_ts="2026-09-21T17:00:00Z",
            body_observed=False,
            attachments_observed=False,
        )


class CountingIngress:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return IntegrationIngressResponse(
            status_code=202 if len(self.payloads) == 1 else 200,
            body={"status": "accepted" if len(self.payloads) == 1 else "duplicate"},
        )


@pytest.mark.parametrize("limit", [1, 3, 100])
def test_connector_enforces_work_bound_when_reader_returns_excess_ids(limit):
    reader = ExcessReader()
    ingress = CountingIngress()
    connector = GmailInboundConnector(reader=reader, ingress=ingress, config=_config())

    result = connector.poll(max_results=limit)

    assert len(reader.reads) == len(ingress.payloads) == result.selected == limit
    assert result.accepted == 1
    assert result.duplicates == limit - 1


@pytest.mark.parametrize("limit", [False, True, 1.5, "1", None, 0, 101])
def test_connector_rejects_non_integer_or_out_of_range_work_bound(limit):
    reader = ExcessReader()
    ingress = CountingIngress()
    connector = GmailInboundConnector(reader=reader, ingress=ingress, config=_config())

    with pytest.raises(ValueError, match="GMAIL_POLL_LIMIT_OUT_OF_RANGE"):
        connector.poll(max_results=limit)

    assert reader.reads == ingress.payloads == []
