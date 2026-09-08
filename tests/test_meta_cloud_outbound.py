import json
from urllib.error import HTTPError

import pytest

from attention_router.adapters.meta_cloud_outbound import (
    MetaCloudOutboundAdapter,
    MetaCloudOutboundConfigError,
    MetaCloudOutboundError,
    MetaCloudOutboundPermanentError,
)
from attention_router.adapters.meta_whatsapp import MetaWhatsAppInboundAdapter
from tests.meta_fixtures import meta_status


def test_meta_cloud_text_request_uses_configured_identifiers():
    adapter = MetaCloudOutboundAdapter(access_token="secret", phone_number_id="phone-1", api_version="v26.0")
    body, headers, url = adapter.build_request("5500000000035", "Teste outbound real Andy")
    assert json.loads(body) == {
        "messaging_product": "whatsapp", "to": "5500000000035", "type": "text",
        "text": {"body": "Teste outbound real Andy"},
    }
    assert headers["Authorization"] == "Bearer secret"
    assert url.endswith("/v26.0/phone-1/messages")


def test_meta_cloud_requires_credentials(monkeypatch):
    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.settings.meta_whatsapp_access_token", None)
    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.settings.meta_access_token", None)
    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.settings.meta_whatsapp_phone_number_id", None)
    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.settings.meta_phone_number_id", None)
    with pytest.raises(MetaCloudOutboundConfigError):
        MetaCloudOutboundAdapter(access_token=None, phone_number_id=None).build_request("5585", "hi")


def test_meta_cloud_response_captures_wamid():
    result = MetaCloudOutboundAdapter(access_token="x", phone_number_id="p").parse_response({"messages": [{"id": "wamid.1"}]})
    assert result.status == "API_ACCEPTED"
    assert result.wamid == "wamid.1"


def test_meta_cloud_response_without_wamid_fails_closed():
    with pytest.raises(MetaCloudOutboundError):
        MetaCloudOutboundAdapter(access_token="x", phone_number_id="p").parse_response({"messages": []})


def test_meta_cloud_interactive_reply_buttons_payload():
    adapter = MetaCloudOutboundAdapter(access_token="x", phone_number_id="p")
    body, _, _ = adapter.build_interactive_request("5500000000035", "Teste", [{"id": "opaque-a", "title": "Autorizar"}, {"id": "opaque-b", "title": "Negar"}])
    payload = json.loads(body)
    assert payload["type"] == "interactive"
    assert [b["reply"] for b in payload["interactive"]["action"]["buttons"]] == [{"id": "opaque-a", "title": "Autorizar"}, {"id": "opaque-b", "title": "Negar"}]


def test_meta_status_preserves_failure_details():
    payload = meta_status()
    status = payload["entry"][0]["changes"][0]["value"]["statuses"][0]
    status.update({"status": "failed", "errors": [{"code": 130497, "title": "blocked", "message": "failed", "error_data": {"details": "detail"}}]})
    parsed = MetaWhatsAppInboundAdapter().iter_statuses(payload)[0]
    assert parsed["error_code"] == 130497
    assert parsed["error_title"] == "blocked"
    assert parsed["error_message"] == "failed"
    assert parsed["error_details"] == "detail"


@pytest.mark.parametrize("status", [401, 400])
def test_meta_cloud_graph_error_captures_safe_diagnostics(monkeypatch, status):
    def fail(*args, **kwargs):
        raise HTTPError("https://graph.facebook.com", status, "rejected", {}, __import__("io").BytesIO(
            b'{"error":{"message":"bad request","type":"OAuthException","code":190,"error_subcode":123,"fbtrace_id":"trace-1"}}'
        ))

    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.request.urlopen", fail)
    with pytest.raises(MetaCloudOutboundPermanentError) as caught:
        MetaCloudOutboundAdapter(access_token="TOKEN", phone_number_id="p", api_version="v26.0").dispatch_outbox(
            type("Outbox", (), {"payload": {"external_actor_id": "5585", "text": "private message"}})()
        )
    assert caught.value.diagnostics == {
        "http_status": status, "error_message": "bad request", "error_type": "OAuthException",
        "error_code": 190, "error_subcode": 123, "fbtrace_id": "trace-1", "api_version": "v26.0",
    }
    assert "TOKEN" not in str(caught.value)
    assert "private message" not in str(caught.value.diagnostics)


def test_meta_cloud_malformed_error_has_no_body_diagnostic(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError("https://graph.facebook.com", 400, "rejected", {}, __import__("io").BytesIO(b"not-json"))

    monkeypatch.setattr("attention_router.adapters.meta_cloud_outbound.request.urlopen", fail)
    with pytest.raises(MetaCloudOutboundPermanentError) as caught:
        MetaCloudOutboundAdapter(access_token="TOKEN", phone_number_id="p").dispatch_outbox(
            type("Outbox", (), {"payload": {"external_actor_id": "5585", "text": "private message"}})()
        )
    assert caught.value.diagnostics["http_status"] == 400
    assert all(value is None for key, value in caught.value.diagnostics.items() if key not in {"http_status", "api_version"})
