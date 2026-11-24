"""Bootstrap only the dedicated private process before shared settings are imported."""
import os
from secrets import token_urlsafe


def main():
    # The shared Settings validator requires a key for the ingress server.
    # Isolate this process from every operational ingress/admin credential.
    os.environ["INTERNAL_INGRESS_HMAC_SECRET"] = token_urlsafe(48)
    os.environ["ADMIN_AUTH_ENABLED"] = "false"
    os.environ.pop("ADMIN_TOKEN", None)
    os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    try:
        from attention_router.web.private_context import main as serve
    except Exception:
        raise SystemExit("CONTEXT_PRIVATE_CONFIGURATION_REFUSED") from None
    finally:
        os.environ.pop("INTERNAL_INGRESS_HMAC_SECRET", None)
    serve()


if __name__ == "__main__":
    main()
