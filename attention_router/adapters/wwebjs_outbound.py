import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from attention_router.config import settings


class WwebjsOutboundError(RuntimeError):
    retryable = True


class WwebjsOutboundPermanentError(WwebjsOutboundError):
    retryable = False


class WwebjsOutboundConfigError(WwebjsOutboundPermanentError):
    pass


@dataclass
class WwebjsOutboundResult:
    status: str
    retryable: bool = False
    response: dict[str, Any] | None = None


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WwebjsOutboundAdapter:
    def __init__(self, url: str | None = None, secret: str | None = None, timeout: float | None = None):
        self.url = url or settings.wwebjs_outbound_url
        self.secret = secret if secret is not None else settings.wwebjs_outbound_hmac_secret
        self.timeout = timeout or settings.wwebjs_outbound_timeout_seconds

    def build_payload(self, outbox) -> dict[str, Any]:
        payload = outbox.payload
        if outbox.action_type == "agent_execution_voice":
            return {
                "schema_version": "1",
                "idempotency_key": outbox.idempotency_key,
                "external_actor_id": payload["external_actor_id"],
                "message_type": "ptt",
                "media_ref": payload["media_ref"],
                "content_sha256": payload["content_sha256"],
                "mime_type": payload["mime_type"],
                "size_bytes": payload["size_bytes"],
                "execution_intent_id": payload["execution_intent_id"],
                "correlation_id": outbox.correlation_id,
                "interaction_id": outbox.interaction_id,
                "outbox_id": outbox.id,
            }
        return {
            "schema_version": "1",
            "idempotency_key": outbox.idempotency_key,
            "external_actor_id": payload["external_actor_id"],
            "message_type": "text",
            "text": payload["text"],
            "correlation_id": outbox.correlation_id,
            "interaction_id": outbox.interaction_id,
            "outbox_id": outbox.id,
        }

    def build_request(self, outbox) -> tuple[bytes, dict[str, str]]:
        if not self.secret:
            raise WwebjsOutboundConfigError("missing WWEBJS_OUTBOUND_HMAC_SECRET")
        body = json.dumps(self.build_payload(outbox), separators=(",", ":"), ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Attention-Timestamp": timestamp,
            "X-Attention-Signature": _signature(self.secret, timestamp, body),
        }
        return body, headers

    def dispatch_outbox(self, outbox) -> WwebjsOutboundResult:
        body, headers = self.build_request(outbox)
        req = request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                data = json.loads(raw.decode() or "{}")
                status = data.get("status")
                if status in {"sent", "already_sent"}:
                    return WwebjsOutboundResult(status=status, response=data)
                raise WwebjsOutboundError(f"unexpected HA status: {status}")
        except error.HTTPError as exc:
            raw = exc.read()
            try:
                data = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                data = {}
            status = data.get("status") or f"http_{exc.code}"
            if exc.code == 409:
                raise WwebjsOutboundPermanentError(status) from exc
            if exc.code in {401, 403}:
                raise WwebjsOutboundConfigError(status) from exc
            if exc.code == 503 or exc.code >= 500:
                raise WwebjsOutboundError(status) from exc
            raise WwebjsOutboundPermanentError(status) from exc
        except TimeoutError as exc:
            raise WwebjsOutboundError("network_timeout") from exc
        except OSError as exc:
            raise WwebjsOutboundError("network_error") from exc


__all__ = [
    "WwebjsOutboundAdapter",
    "WwebjsOutboundConfigError",
    "WwebjsOutboundError",
    "WwebjsOutboundPermanentError",
    "WwebjsOutboundResult",
]
