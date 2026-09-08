"""Narrow control-plane gate for Meta human-authorization button replies."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import ExecutionIntentRow, HumanExecutionAuthorizationRow
from attention_router.infrastructure.repository import audit
from attention_router.platform.human_execution_authorization import decide
from attention_router.platform.meta_observability import provider_message_id_fingerprint


def handle_meta_control_event(session: Session, event, *, enabled: bool, now: datetime | None = None) -> str:
    """Return APPROVE/DENY, DUPLICATE or a fail-closed reason; never executes work."""
    if not enabled:
        return "GATE_DISABLED"
    metadata = event.metadata
    if metadata.get("message_type") != "interactive" or metadata.get("interactive_type") != "button_reply":
        return "NOT_CONTROL_EVENT"
    if str(metadata.get("button_reply_id") or "").startswith("interactive_canary_"):
        return "CANARY_NOT_HUMAN_AUTH"
    context_id = metadata.get("context_id")
    if not context_id:
        return _reject(session, event, "UNRELATED_CONTEXT")
    row = session.scalar(select(HumanExecutionAuthorizationRow).where(HumanExecutionAuthorizationRow.request_wamid == context_id))
    if row is None:
        return _reject(session, event, "UNRELATED_CONTEXT")
    intent = session.get(ExecutionIntentRow, row.execution_intent_id)
    if intent is None:
        return _reject(session, event, "EXECUTION_INTENT_NOT_FOUND", row)
    if intent.state != "FROZEN":
        return _reject(session, event, "EXECUTION_INTENT_NOT_FROZEN", row)
    if intent.scope_fingerprint != row.execution_intent_fingerprint:
        return _reject(session, event, "EXECUTION_INTENT_FINGERPRINT_MISMATCH", row)
    if row.state in {"APPROVED", "DENIED", "CONSUMED"}:
        audit(session, None, "human_execution_auth_control_duplicate", {"authorization_id": row.id, "inbound_wamid_hash": provider_message_id_fingerprint(event.external_event_id), "reason_code": "DUPLICATE"}, correlation_id=row.correlation_id)
        return "DUPLICATE"
    try:
        result = decide(session, authorization_id=row.id, inbound_wamid=event.external_event_id, sender=str(metadata.get("sender") or event.actor_id), context_id=str(context_id), button_id=str(metadata.get("button_reply_id") or ""), now=now)
    except PermissionError as exc:
        reason = str(exc)
        mapped = {"HUMAN_AUTH_BUTTON_UNKNOWN": "UNKNOWN_BUTTON", "HUMAN_AUTH_IDENTITY_OR_CONTEXT_MISMATCH": "UNEXPECTED_APPROVER", "HUMAN_AUTH_EXPIRED_OR_NOT_PENDING": "EXPIRED_OR_INVALID_STATE"}.get(reason, reason)
        return _reject(session, event, mapped, row)
    audit(session, None, "human_execution_auth_control_received", {"authorization_id": row.id, "inbound_wamid_hash": provider_message_id_fingerprint(event.external_event_id), "button_id": metadata.get("button_reply_id")}, correlation_id=row.correlation_id)
    return result


def _reject(session: Session, event, reason: str, row=None) -> str:
    audit(session, None, "human_execution_auth_control_rejected", {"authorization_id": row.id if row else None, "inbound_wamid_hash": provider_message_id_fingerprint(event.external_event_id), "reason_code": reason}, correlation_id=row.correlation_id if row else event.correlation_id)
    return reason
