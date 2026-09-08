"""Persistent human boundary for one Meta transport canary."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import TransportCanaryAuthorizationRow
from attention_router.infrastructure.repository import audit

TRANSPORT = "meta_whatsapp_cloud"
OPERATION = "send_text_canary"
CANARY_TEXT = "Teste outbound real Andy"


def scope_fingerprint(*, transport: str, operation: str, phone_number_id: str, text: str, recipient: str, buttons: list[dict[str, str]] | None = None) -> str:
    scope = {"operation": operation, "phone_number_id": phone_number_id, "recipient": recipient, "text": text, "transport": transport, "buttons": buttons or []}
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def interactive_button_ids(interactive_id: str) -> dict[str, str]:
    seed = hashlib.sha256(interactive_id.encode()).hexdigest()[:20]
    return {"approve": f"interactive_canary_{seed}_approve", "deny": f"interactive_canary_{seed}_deny"}


def prepare_interactive_canary_authorization(session: Session, *, phone_number_id: str, recipient: str, body: str, buttons: list[dict[str, str]], correlation_id: str, ttl_seconds: int = 300, now: datetime | None = None, canary_id: str | None = None) -> TransportCanaryAuthorizationRow:
    now = now or datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    canary_id = canary_id or str(uuid4())
    fingerprint = scope_fingerprint(transport=TRANSPORT, operation="send_interactive_canary", phone_number_id=phone_number_id, text=body, recipient=recipient, buttons=buttons)
    row = TransportCanaryAuthorizationRow(id=canary_id, transport=TRANSPORT, operation="send_interactive_canary", phone_number_id=phone_number_id, text=body, recipient=recipient, scope_fingerprint=fingerprint, status="PREPARED", valid_from=now, expires_at=expires, correlation_id=correlation_id, created_at=now, updated_at=now)
    session.add(row)
    audit(session, None, "transport_interactive_canary_prepared", {"authorization_id": canary_id, "scope_fingerprint": fingerprint, "button_ids": [b["id"] for b in buttons], "expires_at": expires.isoformat()}, correlation_id=correlation_id)
    session.flush()
    return row


def prepare_transport_canary_authorization(session: Session, *, phone_number_id: str, correlation_id: str, ttl_seconds: int = 300, now: datetime | None = None) -> TransportCanaryAuthorizationRow:
    now = now or datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    recipient = "PENDING_OPERATOR_CONFIRMATION"
    fingerprint = scope_fingerprint(transport=TRANSPORT, operation=OPERATION, phone_number_id=phone_number_id, text=CANARY_TEXT, recipient=recipient)
    row = TransportCanaryAuthorizationRow(id=str(uuid4()), transport=TRANSPORT, operation=OPERATION, phone_number_id=phone_number_id, text=CANARY_TEXT, recipient=recipient, scope_fingerprint=fingerprint, status="PREPARED", valid_from=now, expires_at=expires, correlation_id=correlation_id, created_at=now, updated_at=now)
    session.add(row)
    audit(session, None, "transport_canary_authorization_prepared", {"authorization_id": row.id, "transport": TRANSPORT, "operation": OPERATION, "scope_fingerprint": fingerprint, "expires_at": expires.isoformat()}, correlation_id=correlation_id)
    session.flush()
    return row


def authorize_transport_canary(session: Session, authorization_id: str, *, recipient: str, authorized_by: str, buttons: list[dict[str, str]] | None = None, now: datetime | None = None) -> None:
    row = session.get(TransportCanaryAuthorizationRow, authorization_id)
    if row is None or row.status != "PREPARED":
        raise PermissionError("CANARY_AUTHORIZATION_NOT_PREPARED")
    now = now or datetime.now(UTC)
    if now >= row.expires_at:
        raise PermissionError("CANARY_AUTHORIZATION_EXPIRED")
    row.recipient = recipient
    row.scope_fingerprint = scope_fingerprint(transport=row.transport, operation=row.operation, phone_number_id=row.phone_number_id, text=row.text, recipient=recipient, buttons=buttons)
    row.status = "AUTHORIZED"
    row.authorized_by = authorized_by
    row.authorized_at = now
    row.updated_at = now
    audit(session, None, "transport_canary_authorization_granted", {"authorization_id": row.id, "scope_fingerprint": row.scope_fingerprint, "authorized_by": authorized_by}, correlation_id=row.correlation_id)
    session.flush()


def consume_transport_canary_authorization(session: Session, authorization_id: str, *, transport: str, operation: str, phone_number_id: str, text: str, recipient: str, buttons: list[dict[str, str]] | None = None, now: datetime | None = None) -> TransportCanaryAuthorizationRow:
    now = now or datetime.now(UTC)
    fingerprint = scope_fingerprint(transport=transport, operation=operation, phone_number_id=phone_number_id, text=text, recipient=recipient, buttons=buttons)
    result = session.execute(update(TransportCanaryAuthorizationRow).where(TransportCanaryAuthorizationRow.id == authorization_id, TransportCanaryAuthorizationRow.status == "AUTHORIZED", TransportCanaryAuthorizationRow.scope_fingerprint == fingerprint, TransportCanaryAuthorizationRow.expires_at > now).values(status="CONSUMED", consumed_at=now, updated_at=now))
    if result.rowcount != 1:
        raise PermissionError("CANARY_AUTHORIZATION_INVALID_OR_ALREADY_CONSUMED")
    row = session.get(TransportCanaryAuthorizationRow, authorization_id)
    audit(session, None, "transport_canary_authorization_consumed", {"authorization_id": authorization_id, "scope_fingerprint": fingerprint}, correlation_id=row.correlation_id)
    return row
