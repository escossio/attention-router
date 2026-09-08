import hashlib
import hmac
import time

from attention_router.config import settings


def verify_internal_signature(body: bytes, timestamp: str | None, signature: str | None) -> tuple[bool, str]:
    if not timestamp or not signature:
        return False, "missing"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "invalid_timestamp"
    skew = abs(int(time.time()) - ts)
    if skew > settings.internal_ingress_max_skew_seconds:
        return False, "timestamp_skew"
    if not signature.startswith("sha256="):
        return False, "invalid_format"
    secret = settings.internal_ingress_hmac_secret
    if not secret:
        return False, "missing_secret"
    signed = timestamp.encode("utf-8") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, f"sha256={expected}"):
        return False, "invalid_signature"
    return True, "ok"
