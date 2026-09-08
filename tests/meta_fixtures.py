import json

from attention_router.web.meta_security import sign_meta_body


APP_SECRET = "synthetic-test-app-secret"
VERIFY_TOKEN = "synthetic-verify-token"


def meta_text_message(message_id: str = "wamid.synthetic.1", sender: str = "15550000001", text: str = "hello") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_synthetic",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550009999",
                                "phone_number_id": "phone_synthetic",
                            },
                            "contacts": [{"profile": {"name": "Synthetic Sender"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1723060800",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def meta_batch() -> dict:
    payload = meta_text_message("wamid.synthetic.a", "15550000001", "first")
    payload["entry"][0]["changes"][0]["value"]["messages"].append(
        {
            "from": "15550000002",
            "id": "wamid.synthetic.b",
            "timestamp": "1723060801",
            "text": {"body": "second"},
            "type": "text",
        }
    )
    payload["entry"][0]["changes"][0]["value"]["contacts"].append(
        {"profile": {"name": "Second Sender"}, "wa_id": "15550000002"}
    )
    return payload


def meta_status(message_id: str = "wamid.synthetic.status") -> dict:
    payload = meta_text_message()
    value = payload["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value.pop("contacts")
    value["statuses"] = [
        {
            "id": message_id,
            "status": "delivered",
            "timestamp": "1723060802",
            "recipient_id": "15550000001",
        }
    ]
    return payload


def meta_non_text(message_id: str = "wamid.synthetic.image") -> dict:
    payload = meta_text_message(message_id)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.pop("text")
    message["type"] = "image"
    message["image"] = {"id": "media_synthetic", "mime_type": "image/jpeg"}
    return payload


def body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signed_headers(raw_body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    return {"X-Hub-Signature-256": sign_meta_body(raw_body, secret), "Content-Type": "application/json"}
