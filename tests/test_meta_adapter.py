from attention_router.adapters.meta_whatsapp import MetaWhatsAppInboundAdapter
from tests.meta_fixtures import meta_batch, meta_non_text, meta_status, meta_text_message


def test_meta_text_normalization_schema_v1():
    event = MetaWhatsAppInboundAdapter().normalize(meta_text_message())
    assert event.schema_version == "1"
    assert event.source == "meta_whatsapp"
    assert event.external_event_id == "wamid.synthetic.1"
    assert event.actor_id == "15550000001"
    assert event.content == "hello"
    assert event.metadata["message_type"] == "text"


def test_meta_text_normalization_preserves_cloud_provenance():
    event = MetaWhatsAppInboundAdapter().normalize(meta_text_message())
    assert event.external_event_id == "wamid.synthetic.1"
    assert event.content == "hello"
    assert event.metadata["object"] == "whatsapp_business_account"
    assert event.metadata["field"] == "messages"
    assert event.metadata["waba_id"] == "waba_synthetic"
    assert event.metadata["phone_number_id"] == "phone_synthetic"
    assert event.metadata["sender"] == "15550000001"
    assert event.metadata["message_type"] == "text"


def test_meta_normalize_many_multiple_messages():
    events = MetaWhatsAppInboundAdapter().normalize_many(meta_batch())
    assert [event.external_event_id for event in events] == ["wamid.synthetic.a", "wamid.synthetic.b"]


def test_meta_status_not_normalized_as_message():
    adapter = MetaWhatsAppInboundAdapter()
    assert adapter.normalize_many(meta_status()) == []
    assert adapter.iter_statuses(meta_status())[0]["status"] == "delivered"


def _status_payload(status="failed", *, errors=None, include_errors=True):
    payload = meta_status()
    status_event = payload["entry"][0]["changes"][0]["value"]["statuses"][0]
    status_event["status"] = status
    if include_errors:
        status_event["errors"] = errors
    return payload


def test_meta_failed_status_preserves_full_safe_error():
    result = MetaWhatsAppInboundAdapter().iter_statuses(
        _status_payload(
            errors=[
                {
                    "code": 131000,
                    "error_subcode": 2494010,
                    "title": "Message undeliverable",
                    "message": "The message could not be delivered",
                    "error_data": {"details": "Recipient unavailable"},
                    "fbtrace_id": "trace-1",
                }
            ]
        )
    )[0]
    assert result["errors"] == [
        {
            "code": 131000,
            "error_subcode": 2494010,
            "title": "Message undeliverable",
            "message": "The message could not be delivered",
            "error_data": {"details": "Recipient unavailable"},
            "fbtrace_id": "trace-1",
        }
    ]
    assert result["error_code"] == 131000
    assert result["error_subcode"] == 2494010
    assert result["error_fbtrace_id"] == "trace-1"


def test_meta_failed_status_accepts_partial_absent_and_empty_errors():
    adapter = MetaWhatsAppInboundAdapter()
    partial = adapter.iter_statuses(_status_payload(errors=[{"code": 1}, {}, "ignored"]))[0]
    absent = adapter.iter_statuses(_status_payload(include_errors=False))[0]
    empty = adapter.iter_statuses(_status_payload(errors=[]))[0]
    assert partial["errors"] == [{"code": 1}, {}]
    assert absent["errors"] == []
    assert empty["errors"] == []


def test_meta_failed_status_preserves_multiple_errors():
    result = MetaWhatsAppInboundAdapter().iter_statuses(
        _status_payload(errors=[{"code": 1, "title": "first"}, {"code": 2, "message": "second"}])
    )[0]
    assert result["errors"] == [
        {"code": 1, "title": "first"},
        {"code": 2, "message": "second"},
    ]
    assert result["error_code"] == 1


def test_meta_status_success_progression_is_unchanged():
    adapter = MetaWhatsAppInboundAdapter()
    for status in ("sent", "delivered", "read"):
        result = adapter.iter_statuses(_status_payload(status, include_errors=False))[0]
        assert result["id"] == "wamid.synthetic.status"
        assert result["status"] == status
        assert result["timestamp"] == "1723060802"
        assert result["recipient_id"] == "15550000001"
        assert result["errors"] == []


def test_meta_status_errors_drop_unapproved_and_nested_sensitive_fields():
    result = MetaWhatsAppInboundAdapter().iter_statuses(
        _status_payload(
            errors=[
                {
                    "code": 1,
                    "access_token": "do-not-persist",
                    "Authorization": "Bearer do-not-persist",
                    "raw_webhook_body": {"messages": [{"text": "private"}]},
                    "error_data": {
                        "details": "safe diagnostic",
                        "user_message": "do-not-persist",
                        "secret": "do-not-persist",
                    },
                }
            ]
        )
    )[0]
    assert result["errors"] == [
        {"code": 1, "error_data": {"details": "safe diagnostic"}}
    ]


def test_meta_non_text_is_normalized_without_media_download():
    event = MetaWhatsAppInboundAdapter().normalize(meta_non_text())
    assert event.content == "[unsupported WhatsApp message type: image]"
    assert event.metadata["message_type"] == "image"


def test_meta_interactive_button_reply_is_normalized_without_execution_intent():
    payload = meta_text_message("wamid.button", "15550000001", "ignored")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message.update({"type": "interactive", "context": {"id": "wamid.outbound"}, "interactive": {"type": "button_reply", "button_reply": {"id": "interactive_canary_abc_approve", "title": "Autorizar"}}})
    event = MetaWhatsAppInboundAdapter().normalize(payload)
    assert event.metadata["context_id"] == "wamid.outbound"
    assert event.metadata["button_reply_id"] == "interactive_canary_abc_approve"
    assert event.metadata["canonical_action_intent"] == "TEST_APPROVE"


def test_meta_interactive_button_reply_preserves_all_correlation_fields():
    payload = meta_text_message("wamid.button.2", "15550000001", "ignored")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message.update({"type": "interactive", "context": {"id": "wamid.interactive.outbound"}, "interactive": {"type": "button_reply", "button_reply": {"id": "opaque-button", "title": "Autorizar"}}})
    metadata = MetaWhatsAppInboundAdapter().normalize(payload).metadata
    assert metadata["message_type"] == "interactive"
    assert metadata["interactive_type"] == "button_reply"
    assert metadata["button_reply_id"] == "opaque-button"
    assert metadata["button_reply_title"] == "Autorizar"
    assert metadata["context_id"] == "wamid.interactive.outbound"
    assert metadata["canonical_action_intent"] is None
