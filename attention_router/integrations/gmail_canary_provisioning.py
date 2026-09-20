from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import secrets
from uuid import uuid4

from attention_router.integrations.admission import provision_installation
from attention_router.integrations.tenant_binding import (
    INBOUND_SCOPE,
    CredentialRecord,
    IntegrationBinding,
    credential_digest,
)


@dataclass(frozen=True, slots=True)
class GmailCanaryInstallation:
    binding: IntegrationBinding
    credential: CredentialRecord
    raw_secret: str = field(repr=False)


def build_gmail_canary_installation(
    *,
    tenant_id: str,
    audience: str,
    instance_id: str,
    account_id: str | None,
    lifetime_hours: int = 24,
    now: datetime | None = None,
) -> GmailCanaryInstallation:
    if not 1 <= lifetime_hours <= 168:
        raise ValueError("GMAIL_CANARY_CREDENTIAL_LIFETIME_OUT_OF_RANGE")

    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("GMAIL_CANARY_NOW_MUST_BE_TIMEZONE_AWARE")
    stamp = stamp.astimezone(UTC)

    binding_id = f"gmail-canary-{uuid4().hex}"
    credential_id = f"gmail-canary-cred-{uuid4().hex}"
    raw_secret = secrets.token_urlsafe(32)
    scopes = frozenset({INBOUND_SCOPE})

    binding = IntegrationBinding(
        binding_id=binding_id,
        audience=audience,
        tenant_id=tenant_id,
        kind="CHANNEL",
        name="channel.email",
        instance_id=instance_id,
        account_id=account_id,
        active=True,
        scopes=scopes,
    )
    credential = CredentialRecord(
        credential_id=credential_id,
        digest=credential_digest(raw_secret),
        binding_id=binding_id,
        not_before=stamp - timedelta(minutes=1),
        expires_at=stamp + timedelta(hours=lifetime_hours),
        revoked=False,
        scopes=scopes,
    )
    return GmailCanaryInstallation(
        binding=binding,
        credential=credential,
        raw_secret=raw_secret,
    )


def write_secret_file(path: Path, secret: str) -> None:
    if not path.is_absolute():
        raise ValueError("GMAIL_CANARY_SECRET_PATH_MUST_BE_ABSOLUTE")
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("GMAIL_CANARY_SECRET_PARENT_NOT_FOUND")
    if path.exists():
        raise FileExistsError(path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "ATTENTION_ROUTER_INTEGRATION_BEARER="
                + secret
                + "\n"
            )
        os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def provision_gmail_canary(
    session_factory,
    *,
    tenant_id: str,
    audience: str,
    instance_id: str,
    account_id: str | None,
    secret_path: Path,
    lifetime_hours: int = 24,
    now: datetime | None = None,
) -> GmailCanaryInstallation:
    installation = build_gmail_canary_installation(
        tenant_id=tenant_id,
        audience=audience,
        instance_id=instance_id,
        account_id=account_id,
        lifetime_hours=lifetime_hours,
        now=now,
    )

    write_secret_file(secret_path, installation.raw_secret)
    try:
        provision_installation(
            session_factory,
            installation.binding,
            installation.credential,
        )
    except Exception:
        secret_path.unlink(missing_ok=True)
        raise
    return installation


__all__ = [
    "GmailCanaryInstallation",
    "build_gmail_canary_installation",
    "provision_gmail_canary",
    "write_secret_file",
]
