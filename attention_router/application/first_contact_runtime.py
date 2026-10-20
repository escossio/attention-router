"""Glue to existing intents/outbox; no parallel sender or authorization engine."""

from sqlalchemy import func, select, update

from attention_router.application.first_contact import (
    FirstContactConflict, bind_outbox, confirm_delivery, delivery_scope,
    mark_sending, observe_outbox,
)
from attention_router.config import settings
from attention_router.domain.models import now_utc
from attention_router.infrastructure.first_contact_models import (
    FirstContactAttemptRow, FirstContactPresentationRow,
)
from attention_router.infrastructure.models import AuditEventRow, AgentExecutionIntentRow, OutboxMessageRow
from attention_router.infrastructure.repository import audit


def decision_has_presentation(session, decision) -> bool:
    if decision is None or decision.response_introduction_included is not True:
        return False
    return any(
        (row.payload or {}).get("decision_id") == decision.id
        for row in session.scalars(select(AuditEventRow).where(
            AuditEventRow.interaction_id == decision.interaction_id,
            AuditEventRow.event_type == "first_contact.generated",
        ))
    )


def attach_presentation(session, decision, intent, outbox) -> None:
    if not decision_has_presentation(session, decision):
        return
    bind_outbox(session, decision.id, intent.id, outbox.id)
    expected = delivery_scope(session, outbox.id)
    if expected is None:
        raise FirstContactConflict("FIRST_CONTACT_OUTBOX_LINK_REQUIRED")
    outbox.payload = {**outbox.payload, "first_contact": expected}


def validate_presentation_outbox(session, decision, outbox) -> None:
    if outbox.action_type == "agent_execution_voice" and outbox.idempotency_key.endswith(":audio"):
        return  # The parent text effect alone carries presentation proof.
    metadata = (outbox.payload or {}).get("first_contact")
    if metadata is None and not decision_has_presentation(session, decision):
        return
    expected = delivery_scope(session, outbox.id)
    if expected is None or metadata != expected:
        raise FirstContactConflict("FIRST_CONTACT_OUTBOX_METADATA_MISMATCH")


def observe_presentation_outbox(
    session, outbox, *, claimed: bool = False, known_before_effect: bool = False,
) -> None:
    if not (outbox.payload or {}).get("first_contact"):
        return  # OFF/old schema: do not query a table that is not deployed.
    try:
        if claimed:
            expected = delivery_scope(session, outbox.id)
            if expected is None or expected != outbox.payload["first_contact"]:
                raise FirstContactConflict("FIRST_CONTACT_OUTBOX_METADATA_MISMATCH")
            mark_sending(session, outbox.id)
        else:
            observe_outbox(session, outbox.id, known_before_effect=known_before_effect)
    except FirstContactConflict:
        if claimed:
            raise  # Claim boundary quarantines the row; it must not be dispatched.
        # Preserve the unresolved presentation and keep the existing worker alive.
        audit(session, outbox.interaction_id, "first_contact.outcome_integrity_denied",
              {"outbox_id": outbox.id}, origin="first_contact")


def reconcile_proven_text_outbox(session, outbox_id: str, proof) -> bool:
    """Finish only a proven inaugural text effect; never dispatch or replay it.

    External send can succeed while a local HTTP response fails. An authenticated
    Transport receipt with ACK >= 2 resolves that ambiguity without repeating
    the original message, and restores the primary text for optional TTS.
    """
    from attention_router.adapters.first_contact_delivery import VerifiedDeliveryProof
    from attention_router.infrastructure.models import AgentDecisionRow

    if (not isinstance(proof, VerifiedDeliveryProof) or proof.status != "SENT"
            or proof.ack is None or proof.ack < 2 or not proof.message_reference):
        return False
    outbox = session.get(OutboxMessageRow, outbox_id)
    if (outbox is None or outbox.status != "AMBIGUOUS"
            or outbox.action_type != "agent_execution_text"
            or outbox.destination != "local_transport"
            or outbox.last_error != "TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS"
            or not (outbox.payload or {}).get("first_contact")):
        return False
    expected = delivery_scope(session, outbox_id)
    if expected is None or any(getattr(proof, field, None) != value
                               for field, value in expected.items()):
        return False
    attempt = session.scalar(select(FirstContactAttemptRow).where(
        FirstContactAttemptRow.outbox_id == outbox_id,
        FirstContactAttemptRow.state == "CONFIRMED",
        FirstContactAttemptRow.message_reference == proof.message_reference,
        FirstContactAttemptRow.ack >= 2,
    ))
    intent = session.get(AgentExecutionIntentRow, outbox.execution_intent_id)
    decision = session.get(AgentDecisionRow, intent.agent_decision_id) if intent else None
    if (attempt is None or intent is None or decision is None
            or decision.response_introduction_included is not True
            or outbox.causation_id != decision.id
            or intent.id != expected["execution_intent_id"]
            or intent.status != "BLOCKED"
            or intent.blocked_reason != "TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS"
            or outbox.payload.get("first_contact") != expected):
        return False
    # Authentication came from confirm_delivery, not the local HTTP transport.
    outbox.status = "DONE"
    outbox.completed_at = now_utc()
    outbox.last_error = None
    intent.status = "SENT"
    intent.blocked_reason = None
    intent.execution_allowed = True
    intent.external_delivery_allowed = True
    audit(session, outbox.interaction_id, "first_contact.proven_text_outbox_reconciled",
          {"outbox_id": outbox.id, "proof_sha256": proof.body_sha256,
           "ack": proof.ack, "no_replay": True}, origin="first_contact")
    return True


def reconcile_presentations(session, *, adapter=None, limit: int = 10) -> int:
    """Bounded authenticated read of receipts; never enqueue or retry an effect."""
    if not settings.first_contact_enabled:
        return 0
    from attention_router.adapters.first_contact_delivery import FirstContactDeliveryAdapter

    adapter = adapter or FirstContactDeliveryAdapter()
    rows = session.scalars(select(FirstContactAttemptRow).join(
        FirstContactPresentationRow,
        FirstContactPresentationRow.id == FirstContactAttemptRow.presentation_id,
    ).where(
        FirstContactPresentationRow.tenant_id == settings.first_contact_tenant_id,
        FirstContactPresentationRow.source_account == settings.first_contact_source_account,
        FirstContactAttemptRow.state.in_(["QUEUED", "SENDING", "SENT", "AMBIGUOUS"]),
        FirstContactAttemptRow.outbox_id.is_not(None),
    ).order_by(
        func.coalesce(FirstContactAttemptRow.last_probed_at, FirstContactAttemptRow.created_at),
        FirstContactAttemptRow.id,
    ).limit(limit)).all()
    identifiers = [row.outbox_id for row in rows]
    session.commit()
    confirmed = 0
    for outbox_id in identifiers:
        try:
            # Rotate even invalid/stale scopes before deeper validation. A
            # corrupt old item must not starve unrelated pending receipts.
            session.execute(update(FirstContactAttemptRow).where(
                FirstContactAttemptRow.outbox_id == outbox_id,
                FirstContactAttemptRow.presentation_id.in_(
                    select(FirstContactPresentationRow.id).where(
                        FirstContactPresentationRow.tenant_id == settings.first_contact_tenant_id,
                        FirstContactPresentationRow.source_account == settings.first_contact_source_account,
                    )
                ),
            ).values(last_probed_at=now_utc()))
            session.commit()
            observe_outbox(session, outbox_id)
            expected = delivery_scope(session, outbox_id)
            session.commit()  # New presentation locks never cross HTTP.
            if expected is None:
                continue
            proof = adapter.probe(expected)
            if proof is not None:
                confirmed += bool(confirm_delivery(session, outbox_id, proof))
                reconcile_proven_text_outbox(session, outbox_id, proof)
            session.commit()
        except Exception:
            # Missing/invalid proof is not failure-before-effect or retry permission.
            session.rollback()
    return confirmed
