import hashlib
import hmac


def sign_meta_body(body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_meta_signature(body: bytes, header_value: str | None, app_secret: str | None) -> bool:
    if not header_value or not app_secret:
        return False
    if not header_value.startswith("sha256="):
        return False
    expected = sign_meta_body(body, app_secret)
    return hmac.compare_digest(header_value, expected)
