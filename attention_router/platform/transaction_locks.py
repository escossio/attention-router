"""Shared transaction-scoped gates for production attempt state transitions."""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session


def _signed_bigint(namespace: str, value: str) -> int:
    raw = hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:8]
    unsigned = int.from_bytes(raw, byteorder="big", signed=False)
    return unsigned if unsigned < 2**63 else unsigned - 2**64


def _advisory_xact_lock(session: Session, namespace: str, value: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _signed_bigint(namespace, value)},
    )


def acquire_meta_attempt_gate(session: Session, outbox_message_id: str) -> None:
    """Serialize reservation/acceptance before either path acquires row locks."""

    _advisory_xact_lock(session, "meta-attempt", outbox_message_id)


def acquire_meta_provider_gate(session: Session, provider_message_id: str) -> None:
    """Serialize durable receipt and API acceptance for one provider message."""

    _advisory_xact_lock(session, "meta-provider", provider_message_id)
