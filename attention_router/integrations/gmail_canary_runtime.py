from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import secrets


@dataclass(frozen=True, slots=True)
class GmailCanaryRuntimeSecrets:
    postgres_password: str = field(repr=False)
    internal_ingress_hmac_secret: str = field(repr=False)


def generate_runtime_secrets() -> GmailCanaryRuntimeSecrets:
    return GmailCanaryRuntimeSecrets(
        postgres_password=secrets.token_urlsafe(32),
        internal_ingress_hmac_secret=secrets.token_urlsafe(32),
    )


def render_gmail_canary_env(
    *,
    secrets_bundle: GmailCanaryRuntimeSecrets,
) -> str:
    postgres_user = "attention_router_canary"
    postgres_db = "attention_router_canary"
    database_url = (
        "postgresql+psycopg://"
        f"{postgres_user}:{secrets_bundle.postgres_password}"
        f"@db:5432/{postgres_db}"
    )
    values = {
        "APP_NAME": "Attention Router Gmail Canary",
        "APP_ENV": "private",
        "POSTGRES_DB": postgres_db,
        "POSTGRES_USER": postgres_user,
        "POSTGRES_PASSWORD": secrets_bundle.postgres_password,
        "DATABASE_URL": database_url,
        "ADMIN_AUTH_ENABLED": "false",
        "INTERNAL_INGRESS_HMAC_SECRET": (
            secrets_bundle.internal_ingress_hmac_secret
        ),
        "INGRESS_HTTP_HOST": "0.0.0.0",
        "INGRESS_HTTP_PORT": "18101",
        "INTEGRATION_INGRESS_ENABLED": "false",
        "INTEGRATION_INGRESS_AUDIENCE": "andy-gmail-canary",
        "INTEGRATION_DISPATCH_ENABLED": "false",
        "INTEGRATION_DISPATCH_BATCH_SIZE": "5",
        "WORKER_POLL_INTERVAL_SECONDS": "1",
        "LOCAL_TRANSPORT_OUTBOUND_URL": (
            "http://127.0.0.1:1/internal/send"
        ),
        "LOCAL_TRANSPORT_OUTBOUND_TIMEOUT_SECONDS": "0.2",
        "WWEBJS_OUTBOUND_URL": "http://127.0.0.1:1/internal/send",
        "HUMAN_IDENTITY_ENABLED": "false",
        "DEVICE_BOOTSTRAP_ENABLED": "false",
        "CLIENT_SESSION_ENABLED": "false",
        "CLIENT_LOCATION_ENABLED": "false",
        "META_WHATSAPP_ENABLED": "false",
        "META_WEBHOOK_DISPATCH_ENABLED": "false",
        "TTS_ENABLED": "false",
        "STT_ENABLED": "false",
        "ANDY_BEHAVIOR_ENABLED": "false",
        "ANDY_AGENT_ENABLED": "false",
        "OWNER_CONTROL_SEMANTIC_ENABLED": "false",
        "PERSISTENT_MEMORY_ENABLED": "false",
        "MEMORY_INGESTION_ENABLED": "false",
        "MEMORY_CONTEXT_ENABLED": "false",
        "PERSONAL_CONTEXT_RUNTIME_ENABLED": "false",
        "PERSONAL_CONTEXT_RECOMMENDATION_DELIVERY_ENABLED": "false",
        "PERSONAL_CONTEXT_SUGGESTION_DELIVERY_ENABLED": "false",
        "PERSONAL_CONTEXT_AUTHORITY_RUNTIME_ENABLED": "false",
        "PERSONAL_CONTEXT_MATERIALIZATION_RUNTIME_ENABLED": "false",
        "AGENT_DECISION_PIPELINE_ENABLED": "false",
        "AGENT_RESPONSE_REVIEW_ENABLED": "false",
        "AGENT_EXECUTION_ENABLED": "false",
        "EXTERNAL_DELIVERY_ENABLED": "false",
        "AUTONOMOUS_EXECUTION_ENABLED": "false",
        "SYNTHETIC_TEST_DRIVER_ENABLED": "false",
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def set_gmail_canary_integration_enabled(
    path: Path,
    *,
    enabled: bool,
) -> None:
    if not path.is_absolute():
        raise ValueError("GMAIL_CANARY_ENV_PATH_MUST_BE_ABSOLUTE")
    if not path.exists() or not path.is_file():
        raise ValueError("GMAIL_CANARY_ENV_NOT_FOUND")

    lines = path.read_text(encoding="utf-8").splitlines()
    keys = {
        "INTEGRATION_INGRESS_ENABLED": 0,
        "INTEGRATION_DISPATCH_ENABLED": 0,
    }
    target = "true" if enabled else "false"
    rendered: list[str] = []
    for line in lines:
        replaced = False
        for key in keys:
            prefix = key + "="
            if line.startswith(prefix):
                keys[key] += 1
                rendered.append(prefix + target)
                replaced = True
                break
        if not replaced:
            rendered.append(line)

    if any(count != 1 for count in keys.values()):
        raise ValueError("GMAIL_CANARY_ENV_INTEGRATION_FLAGS_INVALID")

    temporary = path.with_name(
        path.name + ".tmp-" + secrets.token_hex(8)
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rendered) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_gmail_canary_env(
    path: Path,
    *,
    secrets_bundle: GmailCanaryRuntimeSecrets | None = None,
) -> None:
    if not path.is_absolute():
        raise ValueError("GMAIL_CANARY_ENV_PATH_MUST_BE_ABSOLUTE")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError("GMAIL_CANARY_ENV_PARENT_NOT_FOUND")
    if path.exists():
        raise FileExistsError(path)

    rendered = render_gmail_canary_env(
        secrets_bundle=secrets_bundle or generate_runtime_secrets()
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.chmod(path, 0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise


__all__ = [
    "GmailCanaryRuntimeSecrets",
    "generate_runtime_secrets",
    "render_gmail_canary_env",
    "set_gmail_canary_integration_enabled",
    "write_gmail_canary_env",
]
