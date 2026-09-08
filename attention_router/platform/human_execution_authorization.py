"""Canonical human approval; separate from bounded run admission."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import ExecutionIntentRow, HumanExecutionAuthorizationRow
from attention_router.infrastructure.repository import audit
from attention_router.platform.meta_observability import provider_message_id_fingerprint
from attention_router.platform.production_authority import ProductionAuthorityDenied


def fingerprint(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare(session: Session, *, execution_intent_id: str, expected_approver: str, ttl_seconds: int, correlation_id: str, now: datetime | None = None, scope: dict | None = None) -> HumanExecutionAuthorizationRow:
    now = now or datetime.now(UTC)
    intent = session.get(ExecutionIntentRow, execution_intent_id)
    if (intent is None or intent.state != "FROZEN" or not intent.scope_fingerprint
            or not expected_approver or not correlation_id or ttl_seconds <= 0):
        raise PermissionError("EXECUTION_INTENT_NOT_AUTHORITABLE")
    expires_at = now + timedelta(seconds=ttl_seconds)
    if intent.expires_at is not None:
        intent_expiry = intent.expires_at.replace(tzinfo=UTC) if intent.expires_at.tzinfo is None else intent.expires_at.astimezone(UTC)
        expires_at = min(expires_at, intent_expiry)
    if expires_at <= now:
        raise PermissionError("EXECUTION_INTENT_EXPIRED")
    row = HumanExecutionAuthorizationRow(id=str(uuid4()), execution_intent_id=intent.id, execution_intent_fingerprint=intent.scope_fingerprint, scope=scope or {}, scope_fingerprint=None, expected_approver=expected_approver, approval_channel="meta_whatsapp_interactive", state="PREPARED", issued_at=now, expires_at=expires_at, correlation_id=correlation_id, created_at=now, updated_at=now)
    session.add(row)
    audit(session, None, "human_execution_authorization_prepared", {"authorization_id": row.id, "scope_fingerprint": row.scope_fingerprint, "expected_approver": expected_approver}, correlation_id=correlation_id)
    session.flush()
    return row


def request_approval(session: Session, authorization_id: str, request_wamid: str) -> None:
    if not isinstance(request_wamid, str) or not request_wamid or len(request_wamid) > 180:
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    row = session.get(HumanExecutionAuthorizationRow, authorization_id)
    if row is None or row.state != "PREPARED":
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    provider_conflict = False
    try:
        with session.begin_nested():
            row.request_wamid = request_wamid
            row.state = "PENDING_HUMAN_APPROVAL"
            row.updated_at = datetime.now(UTC)
            audit(session, None, "human_execution_authorization_requested", {"authorization_id": authorization_id, "request_wamid_hash": provider_message_id_fingerprint(request_wamid)}, correlation_id=row.correlation_id)
            session.flush()
    except IntegrityError:
        provider_conflict = True
    if provider_conflict:
        raise PermissionError("HUMAN_AUTH_PROVIDER_CORRELATION_CONFLICT") from None


def decide(session: Session, *, authorization_id: str, inbound_wamid: str, sender: str, context_id: str, button_id: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    row = session.get(HumanExecutionAuthorizationRow, authorization_id)
    if row is None or row.state in {"APPROVED", "DENIED", "CONSUMED", "REVOKED"}:
        raise PermissionError("HUMAN_AUTH_ALREADY_DECIDED_OR_MISSING")
    if row.state != "PENDING_HUMAN_APPROVAL":
        raise PermissionError("HUMAN_AUTH_EXPIRED_OR_NOT_PENDING")
    intent = session.get(ExecutionIntentRow, row.execution_intent_id)
    if intent is None:
        raise PermissionError("EXECUTION_INTENT_NOT_FOUND")
    if intent.state != "FROZEN":
        raise PermissionError("EXECUTION_INTENT_NOT_FROZEN")
    if intent.scope_fingerprint != row.execution_intent_fingerprint:
        raise PermissionError("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if intent.authority_profile_id is not None:
        from attention_router.platform.production_bridge import validate_frozen_execution_intent_authority
        try:
            validate_frozen_execution_intent_authority(
                session, parent=intent,
                expected_fingerprint=row.execution_intent_fingerprint,
                effective_response_snapshot=intent.scope["immutable_inputs"]["effective_response_snapshot"],
                now=now,
            )
        except ProductionAuthorityDenied as exc:
            raise PermissionError(str(exc)) from exc
    if now >= row.expires_at:
        raise PermissionError("HUMAN_AUTH_EXPIRED_OR_NOT_PENDING")
    if sender != row.expected_approver or context_id != row.request_wamid:
        raise PermissionError("HUMAN_AUTH_IDENTITY_OR_CONTEXT_MISMATCH")
    decision = row.scope.get("buttons", {}).get(button_id)
    if decision not in {"APPROVE", "DENY"}:
        raise PermissionError("HUMAN_AUTH_BUTTON_UNKNOWN")
    result = session.execute(update(HumanExecutionAuthorizationRow).where(HumanExecutionAuthorizationRow.id == authorization_id, HumanExecutionAuthorizationRow.state == "PENDING_HUMAN_APPROVAL").values(state="APPROVED" if decision == "APPROVE" else "DENIED", decision_at=now, decision_inbound_wamid=inbound_wamid, decision_sender=sender, decision_button_id=button_id, updated_at=now))
    if result.rowcount != 1:
        raise PermissionError("HUMAN_AUTH_ALREADY_DECIDED_OR_MISSING")
    audit(session, None, "human_execution_authorization_decided", {"authorization_id": authorization_id, "decision": decision, "inbound_wamid_hash": provider_message_id_fingerprint(inbound_wamid), "button_id": button_id}, correlation_id=row.correlation_id)
    return decision
