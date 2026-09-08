import json
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

from attention_router.config import settings


class MetaCloudOutboundError(RuntimeError):
    retryable = True

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class MetaCloudOutboundPermanentError(MetaCloudOutboundError):
    retryable = False


class MetaCloudOutboundConfigError(MetaCloudOutboundPermanentError):
    pass


@dataclass
class MetaCloudOutboundResult:
    status: str
    wamid: str | None = field(default=None, repr=False)
    response: dict[str, Any] | None = field(default=None, repr=False)


class MetaCloudOutboundAdapter:
    """Provider for text sends; intentionally not registered in the live worker."""

    def __init__(self, access_token: str | None = None, phone_number_id: str | None = None,
                 api_version: str | None = None, timeout: float = 10.0):
        self.access_token = access_token if access_token is not None else (
            settings.meta_whatsapp_access_token or settings.meta_access_token
        )
        self.phone_number_id = phone_number_id if phone_number_id is not None else (
            settings.meta_whatsapp_phone_number_id or settings.meta_phone_number_id
        )
        self.api_version = api_version or settings.meta_graph_api_version
        self.timeout = timeout

    def build_payload(self, recipient: str, text: str) -> dict[str, Any]:
        if not recipient or not recipient.isdigit():
            raise ValueError("recipient must be a digits-only WhatsApp identifier")
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        return {"messaging_product": "whatsapp", "to": recipient, "type": "text", "text": {"body": text}}

    def build_interactive_payload(self, recipient: str, body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
        if not recipient.isdigit() or not body.strip() or len(buttons) != 2:
            raise ValueError("interactive canary requires recipient, body and exactly two buttons")
        if any(not b.get("id") or not b.get("title") for b in buttons):
            raise ValueError("interactive buttons require id and title")
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
                "type": "interactive", "interactive": {"type": "button", "body": {"text": body},
                "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in buttons]}}}

    def build_interactive_request(self, recipient: str, body: str, buttons: list[dict[str, str]]) -> tuple[bytes, dict[str, str], str]:
        if not self.access_token or not self.phone_number_id:
            raise MetaCloudOutboundConfigError("missing Meta Cloud API credentials or phone number id")
        body = json.dumps(self.build_interactive_payload(recipient, body, buttons), ensure_ascii=False, separators=(",", ":")).encode()
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        return body, {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}, url

    def build_request(self, recipient: str, text: str) -> tuple[bytes, dict[str, str], str]:
        if not self.access_token or not self.phone_number_id:
            raise MetaCloudOutboundConfigError("missing Meta Cloud API credentials or phone number id")
        body = json.dumps(self.build_payload(recipient, text), ensure_ascii=False, separators=(",", ":")).encode()
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        return body, {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}, url

    def parse_response(self, payload: dict[str, Any]) -> MetaCloudOutboundResult:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or not messages[0].get("id"):
            raise MetaCloudOutboundError("Meta response missing messages[].id")
        return MetaCloudOutboundResult(status="API_ACCEPTED", wamid=str(messages[0]["id"]), response=payload)

    def dispatch_outbox(self, outbox) -> MetaCloudOutboundResult:
        body, headers, url = self.build_request(outbox.payload["external_actor_id"], outbox.payload["text"])
        try:
            req = request.Request(url, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=self.timeout) as response:
                return self.parse_response(json.loads(response.read().decode() or "{}"))
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode() or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            graph_error = payload.get("error") if isinstance(payload, dict) else {}
            graph_error = graph_error if isinstance(graph_error, dict) else {}
            diagnostics = {
                "http_status": exc.code,
                "error_message": graph_error.get("message"),
                "error_type": graph_error.get("type"),
                "error_code": graph_error.get("code"),
                "error_subcode": graph_error.get("error_subcode"),
                "fbtrace_id": graph_error.get("fbtrace_id"),
                "api_version": self.api_version,
            }
            code = graph_error.get("code")
            if exc.code in {401, 403, 400}:
                raise MetaCloudOutboundPermanentError(
                    f"meta_http_{exc.code}:{code or 'rejected'}", diagnostics=diagnostics
                ) from exc
            raise MetaCloudOutboundError(f"meta_http_{exc.code}", diagnostics=diagnostics) from exc
        except (TimeoutError, OSError) as exc:
            raise MetaCloudOutboundError("meta_network_error") from exc


__all__ = ["MetaCloudOutboundAdapter", "MetaCloudOutboundConfigError", "MetaCloudOutboundError",
           "MetaCloudOutboundPermanentError", "MetaCloudOutboundResult"]
