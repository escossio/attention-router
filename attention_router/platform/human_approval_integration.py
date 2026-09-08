"""Inert Meta interactive request glue for production human approval."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.adapters.meta_cloud_outbound import MetaCloudOutboundAdapter
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    HumanApprovalDeliveryEvidenceRow,
    HumanExecutionAuthorizationRow,
    MetaCallbackInboxRow,
    MetaDeliveryReconciliationRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.execution_intent_retirement import retire_execution_intent
from attention_router.platform.human_execution_authorization import prepare, request_approval
from attention_router.platform.meta_callback_reconciliation import (
    ADMISSION_GRACE_SECONDS,
    persist_human_approval_delivery_evidence,
)
from attention_router.platform.meta_observability import (
    provider_message_id_fingerprint,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import validate_frozen_execution_intent_authority
from attention_router.platform.transaction_locks import acquire_meta_provider_gate


def prepare_production_human_approval(
    session: Session,
    *,
    execution_intent_id: str,
    expected_approver: str,
    ttl_seconds: int,
    correlation_id: str,
    now: datetime | None = None,
) -> HumanExecutionAuthorizationRow:
    """Bind a PREPARED HEA to one still-valid, exact frozen authority."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    parent = session.execute(
        select(ExecutionIntentRow).where(
            ExecutionIntentRow.id == execution_intent_id
        ).with_for_update()
    ).scalar_one_or_none()
    if parent is None:
        raise PermissionError("EXECUTION_INTENT_NOT_FOUND")
    try:
        validate_frozen_execution_intent_authority(
            session, parent=parent, expected_fingerprint=parent.scope_fingerprint,
            effective_response_snapshot=parent.scope["immutable_inputs"]["effective_response_snapshot"],
            now=timestamp,
        )
    except ProductionAuthorityDenied as exc:
        raise PermissionError(str(exc)) from exc
    buttons = {uuid4().hex: "APPROVE", uuid4().hex: "DENY"}
    return prepare(
        session,
        execution_intent_id=parent.id,
        expected_approver=expected_approver,
        ttl_seconds=ttl_seconds,
        correlation_id=correlation_id,
        now=timestamp,
        scope={"buttons": buttons},
    )


def build_production_human_approval_request(
    session: Session, *, authorization_id: str, now: datetime | None = None,
) -> dict:
    """Build, but never send, the interactive Meta request for a PREPARED HEA."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    hea = session.get(HumanExecutionAuthorizationRow, authorization_id)
    if hea is None or hea.state != "PREPARED":
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    parent = session.get(ExecutionIntentRow, hea.execution_intent_id)
    if parent is None:
        raise PermissionError("EXECUTION_INTENT_NOT_FOUND")
    try:
        authority, endpoint = validate_frozen_execution_intent_authority(
            session, parent=parent, expected_fingerprint=hea.execution_intent_fingerprint,
            effective_response_snapshot=parent.scope["immutable_inputs"]["effective_response_snapshot"],
            now=timestamp,
        )
    except ProductionAuthorityDenied as exc:
        raise PermissionError(str(exc)) from exc
    buttons = hea.scope.get("buttons") if isinstance(hea.scope, dict) else None
    if not isinstance(buttons, dict) or set(buttons.values()) != {"APPROVE", "DENY"}:
        raise PermissionError("HUMAN_AUTH_BUTTON_UNKNOWN")
    rendered_buttons = [
        {"id": button_id, "title": "Aprovar" if action == "APPROVE" else "Negar"}
        for button_id, action in buttons.items()
    ]
    body = (
        f"Aprovar {authority.capability} para {endpoint.canonical_address}? "
        f"Válido até {hea.expires_at.astimezone(UTC).isoformat()}."
    )
    return {
        "authorization_id": hea.id,
        "correlation_id": hea.correlation_id,
        "payload": MetaCloudOutboundAdapter().build_interactive_payload(
            endpoint.canonical_address, body, rendered_buttons,
        ),
        "buttons": rendered_buttons,
    }


def mark_production_human_approval_requested(
    session: Session, *, authorization_id: str, request_wamid: str,
    now: datetime | None = None,
) -> None:
    """Persist the local/transport request correlation and enter PENDING."""
    if not request_wamid:
        raise PermissionError("HUMAN_AUTH_REQUEST_WAMID_MISMATCH")
    acquire_meta_provider_gate(session, request_wamid)
    if session.scalar(
        select(MetaDeliveryReconciliationRow.id).where(
            MetaDeliveryReconciliationRow.provider_message_id == request_wamid
        )
    ) is not None:
        raise PermissionError("HUMAN_AUTH_PROVIDER_SCOPE_COLLISION")
    execution_intent_id = session.scalar(
        select(HumanExecutionAuthorizationRow.execution_intent_id).where(
            HumanExecutionAuthorizationRow.id == authorization_id
        )
    )
    if execution_intent_id is None:
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    parent = session.scalar(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    )
    if parent is None:
        raise PermissionError("EXECUTION_INTENT_NOT_FOUND")
    hea = session.scalar(
        select(HumanExecutionAuthorizationRow)
        .where(HumanExecutionAuthorizationRow.id == authorization_id)
        .with_for_update()
    )
    if hea is None or hea.execution_intent_id != parent.id:
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    try:
        validate_frozen_execution_intent_authority(
            session, parent=parent, expected_fingerprint=hea.execution_intent_fingerprint,
            effective_response_snapshot=parent.scope["immutable_inputs"]["effective_response_snapshot"],
            now=now,
        )
    except ProductionAuthorityDenied as exc:
        raise PermissionError(str(exc)) from exc
    request_approval(session, authorization_id, request_wamid)
    pending_delivery_rows = session.scalars(
        select(MetaCallbackInboxRow).where(
            MetaCallbackInboxRow.provider_message_id == request_wamid,
            MetaCallbackInboxRow.valid.is_(True),
            MetaCallbackInboxRow.provider_status.in_(
                ["sent", "delivered", "read", "failed"]
            ),
        )
    ).all()
    for inbox in pending_delivery_rows:
        persist_human_approval_delivery_evidence(session, inbox=inbox)


def close_failed_production_human_approval(
    session: Session,
    *,
    authorization_id: str,
    expected_request_wamid: str,
    expected_execution_intent_fingerprint: str,
    now: datetime | None = None,
) -> tuple[HumanExecutionAuthorizationRow, ExecutionIntentRow]:
    """Revoke an undeliverable approval and atomically retire its frozen intent."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if not expected_request_wamid:
        raise PermissionError("HUMAN_AUTH_REQUEST_WAMID_MISMATCH")
    acquire_meta_provider_gate(session, expected_request_wamid)
    execution_intent_id = session.scalar(
        select(HumanExecutionAuthorizationRow.execution_intent_id).where(
            HumanExecutionAuthorizationRow.id == authorization_id
        )
    )
    if execution_intent_id is None:
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    locked_parent = session.scalar(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    )
    if locked_parent is None:
        raise PermissionError("EXECUTION_INTENT_NOT_FOUND")
    hea = session.execute(
        select(HumanExecutionAuthorizationRow)
        .where(HumanExecutionAuthorizationRow.id == authorization_id)
        .with_for_update()
    ).scalar_one_or_none()
    if hea is None or hea.execution_intent_id != locked_parent.id:
        raise PermissionError("HUMAN_AUTH_INVALID_REQUEST")
    if hea.request_wamid != expected_request_wamid:
        raise PermissionError("HUMAN_AUTH_REQUEST_WAMID_MISMATCH")
    if (
        not expected_execution_intent_fingerprint
        or hea.execution_intent_fingerprint
        != expected_execution_intent_fingerprint
    ):
        raise PermissionError("EXECUTION_INTENT_FINGERPRINT_MISMATCH")

    status_rows = session.scalars(
        select(HumanApprovalDeliveryEvidenceRow)
        .where(
            HumanApprovalDeliveryEvidenceRow.authorization_id == hea.id,
            HumanApprovalDeliveryEvidenceRow.received_at <= hea.expires_at,
        )
        .order_by(
            HumanApprovalDeliveryEvidenceRow.received_at,
            HumanApprovalDeliveryEvidenceRow.id,
        )
    ).all()
    delivery_proved = any(
        row.provider_status in {"delivered", "read"} for row in status_rows
    )
    failed_status_evidence = next(
        (row for row in status_rows if row.provider_status == "failed"),
        None,
    )
    if delivery_proved or failed_status_evidence is None:
        raise PermissionError("META_DELIVERY_FAILURE_NOT_OBSERVED")
    closure_after = hea.expires_at + timedelta(seconds=ADMISSION_GRACE_SECONDS)
    if timestamp < closure_after:
        raise PermissionError("META_DELIVERY_FAILURE_NOT_STABLE")

    if hea.state == "REVOKED":
        parent = retire_execution_intent(
            session,
            execution_intent_id=hea.execution_intent_id,
            expected_fingerprint=expected_execution_intent_fingerprint,
            now=timestamp,
        )
        return hea, parent
    if hea.state != "PENDING_HUMAN_APPROVAL":
        raise PermissionError("HUMAN_AUTH_NOT_PENDING")

    parent = retire_execution_intent(
        session,
        execution_intent_id=hea.execution_intent_id,
        expected_fingerprint=expected_execution_intent_fingerprint,
        now=timestamp,
    )
    hea.state = "REVOKED"
    hea.updated_at = timestamp
    audit(
        session,
        None,
        "human_execution_authorization_revoked",
        {
            "authorization_id": hea.id,
            "execution_intent_id": parent.id,
            "request_wamid_hash": provider_message_id_fingerprint(
                expected_request_wamid
            ),
            "meta_status_evidence_id": (
                failed_status_evidence.id
            ),
            "reason_code": "META_DELIVERY_FAILED",
        },
        correlation_id=hea.correlation_id,
        previous_state="PENDING_HUMAN_APPROVAL",
        next_state="REVOKED",
    )
    session.flush()
    return hea, parent


__all__ = [
    "build_production_human_approval_request",
    "close_failed_production_human_approval",
    "mark_production_human_approval_requested",
    "prepare_production_human_approval",
]
