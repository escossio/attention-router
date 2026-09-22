"""Provider-neutral terminal human approval decision evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    HumanApprovalDecisionEvidenceRow,
    HumanExecutionAuthorizationRow,
)
from attention_router.infrastructure.repository import audit


class HumanApprovalDecisionError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class HumanApprovalDecisionResult:
    decision: str
    evidence: HumanApprovalDecisionEvidenceRow
    duplicate: bool


def execution_intent_tenant(
    parent: ExecutionIntentRow,
    *,
    allow_default: bool = False,
) -> str:
    target = parent.scope.get("target") if isinstance(parent.scope, dict) else None
    tenant_id = target.get("tenant") if isinstance(target, dict) else None
    if isinstance(tenant_id, str) and tenant_id:
        return tenant_id
    if allow_default:
        return DEFAULT_TENANT_ID
    raise HumanApprovalDecisionError("HUMAN_AUTH_TENANT_UNAVAILABLE")


def _idempotency_key(
    authorization_id: str,
    *,
    channel: str,
    decision_reference: str,
) -> str:
    raw = f"{authorization_id}|{channel}|{decision_reference}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def decide_human_approval(
    session: Session,
    *,
    authorization_id: str,
    decision: str,
    channel: str,
    approver_reference: str,
    tenant_id: str,
    decision_reference: str,
    human_identity_id: str | None = None,
    device_id: str | None = None,
    client_session_id: str | None = None,
    provider_event_reference: str | None = None,
    now: datetime | None = None,
) -> HumanApprovalDecisionResult:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if decision not in {"APPROVE", "DENY"}:
        raise HumanApprovalDecisionError("HUMAN_AUTH_DECISION_UNKNOWN")
    if channel not in {"META_WHATSAPP", "ANDROID_CLIENT"}:
        raise HumanApprovalDecisionError("HUMAN_AUTH_CHANNEL_UNKNOWN")
    if not approver_reference or not decision_reference:
        raise HumanApprovalDecisionError("HUMAN_AUTH_DECISION_EVIDENCE_INVALID")
    if channel == "ANDROID_CLIENT":
        if not human_identity_id or not device_id or not client_session_id:
            raise HumanApprovalDecisionError("HUMAN_AUTH_DECISION_EVIDENCE_INVALID")
        if provider_event_reference is not None:
            raise HumanApprovalDecisionError("HUMAN_AUTH_DECISION_EVIDENCE_INVALID")
    elif not provider_event_reference:
        raise HumanApprovalDecisionError("HUMAN_AUTH_DECISION_EVIDENCE_INVALID")

    row = session.scalar(
        select(HumanExecutionAuthorizationRow)
        .where(HumanExecutionAuthorizationRow.id == authorization_id)
        .with_for_update()
    )
    if row is None:
        raise HumanApprovalDecisionError("HUMAN_AUTH_ALREADY_DECIDED_OR_MISSING")

    existing = session.scalar(
        select(HumanApprovalDecisionEvidenceRow).where(
            HumanApprovalDecisionEvidenceRow.authorization_id == authorization_id
        )
    )
    if existing is not None:
        if (
            row.state == ("APPROVED" if decision == "APPROVE" else "DENIED")
            and existing.decision == decision
            and existing.channel == channel
            and existing.approver_reference == approver_reference
        ):
            return HumanApprovalDecisionResult(decision, existing, True)
        raise HumanApprovalDecisionError("HUMAN_AUTH_ALREADY_DECIDED_OR_MISSING")

    if row.state != "PENDING_HUMAN_APPROVAL":
        raise HumanApprovalDecisionError("HUMAN_AUTH_EXPIRED_OR_NOT_PENDING")

    parent = session.scalar(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == row.execution_intent_id)
        .with_for_update()
    )
    if parent is None:
        raise HumanApprovalDecisionError("EXECUTION_INTENT_NOT_FOUND")
    if parent.state != "FROZEN":
        raise HumanApprovalDecisionError("EXECUTION_INTENT_NOT_FROZEN")
    if parent.scope_fingerprint != row.execution_intent_fingerprint:
        raise HumanApprovalDecisionError("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    expires_at = (
        row.expires_at.replace(tzinfo=UTC)
        if row.expires_at.tzinfo is None
        else row.expires_at.astimezone(UTC)
    )
    if timestamp >= expires_at:
        raise HumanApprovalDecisionError("HUMAN_AUTH_EXPIRED_OR_NOT_PENDING")
    if row.expected_approver != approver_reference:
        raise HumanApprovalDecisionError("HUMAN_AUTH_IDENTITY_OR_CONTEXT_MISMATCH")
    parent_tenant = execution_intent_tenant(
        parent,
        allow_default=channel == "META_WHATSAPP",
    )
    if parent_tenant != tenant_id:
        raise HumanApprovalDecisionError("HUMAN_AUTH_TENANT_MISMATCH")

    evidence = HumanApprovalDecisionEvidenceRow(
        id=str(uuid4()),
        authorization_id=row.id,
        tenant_id=tenant_id,
        decision=decision,
        channel=channel,
        approver_reference=approver_reference,
        human_identity_id=human_identity_id,
        device_id=device_id,
        client_session_id=client_session_id,
        provider_event_reference=provider_event_reference,
        idempotency_key=_idempotency_key(
            row.id,
            channel=channel,
            decision_reference=decision_reference,
        ),
        decided_at=timestamp,
        created_at=timestamp,
    )
    session.add(evidence)
    previous_state = row.state
    row.state = "APPROVED" if decision == "APPROVE" else "DENIED"
    row.decision_at = timestamp
    row.updated_at = timestamp
    audit(
        session,
        None,
        "human_execution_authorization_decided",
        {
            "authorization_id": row.id,
            "decision": decision,
            "decision_channel": channel,
            "decision_evidence_id": evidence.id,
        },
        correlation_id=row.correlation_id,
        previous_state=previous_state,
        next_state=row.state,
        origin="human_approval_decision",
        tenant_id=tenant_id,
        created_at=timestamp,
    )
    session.flush()
    return HumanApprovalDecisionResult(decision, evidence, False)


__all__ = [
    "HumanApprovalDecisionError",
    "HumanApprovalDecisionResult",
    "decide_human_approval",
    "execution_intent_tenant",
]
