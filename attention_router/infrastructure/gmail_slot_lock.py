"""Gmail slot serialization; callers own the transaction and all releases."""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session


def acquire_gmail_slot(session: Session, slot: str, *, wait: bool = True) -> bool:
    """Acquire before any Tenant/ProviderAuthorization/Binding/Credential lock.

    Domain-separated SHA-256 truncated to a signed 64-bit PostgreSQL advisory
    key. Input is the immutable tenant/human/provider/product slot, never a
    secret or mutable account identity. A hash collision only over-serializes
    unrelated slots (or yields BUSY); it cannot grant authority. SQLite is an
    offline functional fallback, not concurrency certification.
    """
    if session.get_bind().dialect.name == "sqlite":
        return True
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("GMAIL_SLOT_LOCK_UNAVAILABLE")
    key = int.from_bytes(
        hashlib.sha256(b"attention-router:gmail-slot:v1:" + slot.encode("ascii")).digest()[:8],
        "big", signed=True,
    )
    function = "pg_advisory_xact_lock" if wait else "pg_try_advisory_xact_lock"
    with session.no_autoflush:
        acquired = session.scalar(text(f"SELECT {function}(:key)"), {"key": key})
    return True if wait else bool(acquired)
