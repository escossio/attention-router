from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import error, request

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AgentResponseReviewRow,
    AutonomyEvaluationRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    EffectBudgetRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    ReadinessResultRow,
    ScenarioRunRow,
)
from attention_router.infrastructure.repository import audit, mask_identifier
from attention_router.application.repetition import suppress_if_repeated
from attention_router.application.owner_reply_grace import grace_allows_interaction
from attention_router.application.owner_automation_control import automation_denial_reason
from attention_router.observability.tracing import current_span, safe_set_attribute, set_outcome, traced_span
from attention_router.platform.execution_safety import (
    DispatchSafetyInput,
    EffectDirection,
    SafetyDenied,
    validate_dispatch_and_bind_outbox_in_transaction,
)


class ExecutionError(ValueError):
    pass


class ExecutionNotFound(ExecutionError):
    pass


class ExecutionBlocked(ExecutionError):
    pass


@dataclass(frozen=True)
class Recipient:
    reference: str
    masked: str


@dataclass(frozen=True)
class TransportProbe:
    ready: bool
    external_delivery_enabled: bool


@dataclass(frozen=True)
class TransportStatusSnapshot:
    """Finite, read-only observation of a transport status endpoint."""

    transport_id: str
    role: str
    state: str
    configured: bool
    reachable: bool
    connected: bool
    ready: bool
    observed_at: datetime
    fresh_until: datetime
    reason_code: str
    reference: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_recipient(session: Session, intent: AgentExecutionIntentRow) -> Recipient:
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    receipt = session.scalar(
        select(InboundEventRow)
        .where(InboundEventRow.interaction_id == decision.interaction_id, InboundEventRow.source == settings.internal_ingress_source)
        .order_by(InboundEventRow.received_at.desc())
    ) if decision else None
    payload = receipt.payload if receipt else {}
    reference = payload.get("external_actor_id") or payload.get("actor_id")
    metadata = payload.get("metadata") or {}
    if not reference or not interaction or payload.get("channel") not in {None, "whatsapp"}:
        raise ExecutionBlocked("RECIPIENT_RESOLUTION_FAILED")
    if str(reference).lower().endswith("@g.us") or metadata.get("is_group") is True or str(reference).lower() == "status@broadcast":
        raise ExecutionBlocked("RECIPIENT_RESOLUTION_FAILED")
    return Recipient(str(reference), mask_identifier(str(reference)))


def transport_status_from_payload(
    payload: dict[str, Any], *, transport_id: str, role: str,
    observed_at: datetime | None = None,
) -> TransportStatusSnapshot:
    observed = (observed_at or _now()).astimezone(timezone.utc)
    if not isinstance(payload, dict):
        return TransportStatusSnapshot(transport_id, role, "UNKNOWN", False, False, False, False,
            observed, observed, "TRANSPORT_STATUS_MALFORMED", f"transport:{transport_id}")
    state = str(payload.get("state") or payload.get("client_state") or "UNKNOWN")
    connected = payload.get("client_state") == "CONNECTED"
    ready = payload.get("ready") is True and connected
    return TransportStatusSnapshot(
        transport_id, role, "READY" if ready else "NOT_READY",
        payload.get("enabled", True) is not False, True, connected, ready,
        observed, observed + timedelta(seconds=settings.transport_status_stale_after),
        "TRANSPORT_READY" if ready else state, f"transport:{transport_id}",
    )


def get_transport_status(*, transport_id: str = "local", role: str = "production", status_url: str | None = None,
                         timeout: float | None = None) -> TransportStatusSnapshot:
    """Read status only; this function has no send/restart/pair capability."""
    try:
        endpoint = status_url or f"{settings.local_transport_outbound_url.rsplit('/internal/send', 1)[0]}/status"
        with request.urlopen(endpoint, timeout=timeout or settings.local_transport_outbound_timeout_seconds) as response:
            body = json.loads(response.read().decode() or "{}")
        snapshot = transport_status_from_payload(body, transport_id=transport_id, role=role)
        return snapshot
    except (OSError, ValueError, error.URLError):
        now = _now()
        return TransportStatusSnapshot(transport_id, role, "UNKNOWN", True, False, False, False,
            now, now, "TRANSPORT_STATUS_UNAVAILABLE", f"transport:{transport_id}")


def probe_transport_status() -> TransportProbe:
    status = get_transport_status()
    return TransportProbe(ready=status.ready, external_delivery_enabled=False)


def probe_transport_ready() -> bool:
    return probe_transport_status().ready


def requires_platform_execution_safety(
    session: Session,
    intent: AgentExecutionIntentRow,
) -> bool:
    """Platform effects are identified structurally, never by a caller toggle."""

    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    event = session.get(InboundEventRow, decision.event_id) if decision else None
    return bool(
        event
        and (
            event.lineage_classification == "SYNTHETIC"
            or event.scenario_run_id is not None
        )
    )


def execution_gate(
    session: Session,
    intent: AgentExecutionIntentRow,
    *,
    transport_ready: bool,
) -> tuple[str, str, Recipient | None]:
    pause_reason = automatic_intent_denial_reason(session, intent)
    if pause_reason:
        return "BLOCKED", pause_reason, None
    if not settings.agent_execution_enabled:
        return "BLOCKED", "AGENT_EXECUTION_DISABLED", None
    if intent.status == "CANCELLED":
        return "DENIED", "INTENT_CANCELLED", None
    if intent.release_status != "RELEASED":
        return "BLOCKED", "INTENT_NOT_RELEASED", None
    if intent.authorization_source == "POLICY_AUTONOMY":
        evaluation = session.get(AutonomyEvaluationRow, intent.autonomy_evaluation_id)
        if not evaluation or not evaluation.automatic_execution_allowed:
            return "DENIED", "AUTONOMY_NOT_ALLOWED", None
    else:
        review = session.get(AgentResponseReviewRow, intent.response_review_id)
        if not review or review.status != "APPROVED":
            return "DENIED", "REVIEW_NOT_APPROVED", None
    try:
        recipient = resolve_recipient(session, intent)
    except ExecutionBlocked as exc:
        return "DENIED", str(exc), None
    lab_permit = None
    if not settings.external_delivery_enabled:
        from attention_router.application.lab_conversation import permit_for_intent
        lab_permit = permit_for_intent(session, intent)
        if lab_permit is None:
            return "BLOCKED", "EXTERNAL_DELIVERY_DISABLED", recipient
    if not transport_ready:
        return "BLOCKED", "TRANSPORT_NOT_READY", recipient
    return "ALLOWED", "READY", recipient


def automatic_intent_denial_reason(
    session: Session, intent: AgentExecutionIntentRow, *, lock=False,
) -> str | None:
    """Only policy-authorized effects; explicit human approval is orthogonal."""
    if intent.authorization_source != "POLICY_AUTONOMY":
        return None
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    if interaction is None:
        return "OWNER_AUTOMATION_SCOPE_UNRESOLVED"
    pause = automation_denial_reason(session, interaction.tenant_id, lock=lock)
    if pause:
        return pause
    from attention_router.application.direct_conversation import direct_conversation_eligibility
    from attention_router.infrastructure.models import InboundEventRow, AutonomyEvaluationRow
    event = session.get(InboundEventRow, decision.event_id, populate_existing=True)
    evaluation = session.get(AutonomyEvaluationRow, intent.autonomy_evaluation_id) if intent.autonomy_evaluation_id else None
    if event is None:
        return "DIRECT_EVENT_MISSING"
    if (event.source == "wwebjs" or (event.payload or {}).get("channel") == "whatsapp") and not (intent.effective_response_snapshot or "").strip():
        return "AUTOMATIC_RESPONSE_EMPTY"
    if event.source == "wwebjs" or (evaluation and evaluation.conversation_contract_version):
        direct = direct_conversation_eligibility(event)
        if not direct.eligible:
            return direct.reason_code
        if event.interaction_id != interaction.id or event.tenant_id != interaction.tenant_id:
            return "DIRECT_INTERACTION_MISMATCH"
        try:
            recipient = resolve_recipient(session, intent)
        except ExecutionBlocked as exc:
            return str(exc)
        if recipient.reference != direct.peer_reference:
            return "DIRECT_RECIPIENT_MISMATCH"
        if evaluation and evaluation.conversation_contract_version:
            from attention_router.application.decision_pipeline import resolve_decision_routing, _blueprint
            from attention_router.application.autonomy import evaluate
            routing = resolve_decision_routing(session, event, interaction)
            policy = routing.policy_version
            if not policy or policy.id != decision.policy_version_id or policy.status != "ACTIVE":
                return "CONVERSATION_POLICY_STALE"
            blueprint, version = _blueprint(session, routing.binding, interaction.tenant_id, explicit_only=True)
            if (not blueprint or not version or blueprint.id != decision.agent_blueprint_id
                or version.version != decision.agent_blueprint_version):
                return "CONVERSATION_BLUEPRINT_STALE"
            current = evaluate(decision, event, interaction, version, policy.config)
            if not current.automatic_execution_allowed:
                return current.reason_code
    return None


def release_intent(
    session: Session,
    intent_id: str,
    operator_reference: str = "operator",
    *,
    transport_ready: bool = False,
) -> AgentExecutionIntentRow:
    intent = session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.id == intent_id).with_for_update())
    if intent is None:
        raise ExecutionNotFound("execution intent not found")
    if intent.status in {"SENT", "CANCELLED"}:
        raise ExecutionBlocked(f"intent cannot be released from {intent.status}")
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    audit(session, decision.interaction_id, "execution.release_requested", {"intent_id": intent.id})
    recipient = resolve_recipient(session, intent)
    if not settings.external_delivery_enabled:
        from attention_router.application.lab_conversation import permit_for_intent
        if permit_for_intent(session, intent) is None:
            raise ExecutionBlocked("EXTERNAL_DELIVERY_DISABLED")
    if not transport_ready:
        raise ExecutionBlocked("TRANSPORT_NOT_READY")
    intent.recipient_reference = recipient.reference
    intent.released_by = operator_reference
    intent.released_at = _now()
    intent.release_status = "RELEASED"
    gate, reason, _ = execution_gate(session, intent, transport_ready=transport_ready)
    if gate != "ALLOWED":
        intent.status = "BLOCKED"
        intent.blocked_reason = reason
        audit(session, decision.interaction_id, "execution.blocked", {"intent_id": intent.id, "reason": reason})
        raise ExecutionBlocked(reason)
    intent.status = "READY"
    intent.blocked_reason = None
    audit(session, decision.interaction_id, "execution.released", {"intent_id": intent.id, "recipient": recipient.masked})
    audit(session, decision.interaction_id, "execution.ready", {"intent_id": intent.id})
    return intent


def _readiness_state_for_scenario(
    session: Session,
    scenario: ScenarioRunRow | None,
    *,
    now: datetime,
) -> str:
    if scenario is None or not scenario.readiness_result_id:
        return "UNKNOWN"
    readiness = session.get(ReadinessResultRow, scenario.readiness_result_id)
    if (
        readiness is None
        or readiness.tenant_id != scenario.tenant_id
        or readiness.dimension != "DOMAIN_READINESS"
        or not readiness.is_current
    ):
        return "UNKNOWN"
    evaluated_at = readiness.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    fresh_until = readiness.evidence_fresh_until
    if fresh_until is None:
        return "STALE"
    if fresh_until.tzinfo is None:
        fresh_until = fresh_until.replace(tzinfo=timezone.utc)
    if (
        (now - evaluated_at).total_seconds() > settings.readiness_result_max_age
        or now > fresh_until
    ):
        return "STALE"
    return readiness.state


def validate_platform_outbox_safety(
    session: Session,
    *,
    intent: AgentExecutionIntentRow,
    outbox: OutboxMessageRow,
    transport_ready: bool,
) -> None:
    consumption = session.scalar(
        select(EffectConsumptionRow).where(
            EffectConsumptionRow.execution_intent_id == intent.id,
            EffectConsumptionRow.state == "RESERVED",
        )
    )
    if consumption is None:
        raise SafetyDenied("EFFECT_BUDGET_MISSING")
    lease = session.get(ExecutionLeaseRow, consumption.execution_lease_id)
    budget = session.get(EffectBudgetRow, consumption.effect_budget_id)
    if lease is None or budget is None:
        raise SafetyDenied("DURABLE_SAFETY_STATE_MISSING")
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    if interaction is None:
        raise SafetyDenied("EXECUTION_INTENT_TENANT_UNRESOLVABLE")
    scenario = session.get(ScenarioRunRow, budget.scenario_run_id)
    timestamp = _now()
    gate, reason, recipient = execution_gate(
        session,
        intent,
        transport_ready=transport_ready,
    )
    if gate != "ALLOWED" or recipient is None:
        raise SafetyDenied(reason)
    payload = outbox.payload or {}
    if payload.get("execution_intent_id") != intent.id:
        raise SafetyDenied("OUTBOX_EXECUTION_INTENT_PAYLOAD_MISMATCH")
    if payload.get("external_actor_id") != recipient.reference:
        raise SafetyDenied("OUTBOX_RECIPIENT_MISMATCH")
    if consumption.direction != EffectDirection.SYSTEM.value:
        raise SafetyDenied("SYSTEM_RESPONSE_REQUIRES_SYSTEM_BUDGET")
    if budget.effect_type != "WHATSAPP_TEXT":
        raise SafetyDenied("SYSTEM_RESPONSE_EFFECT_TYPE_MISMATCH")
    if consumption.logical_effect_id != outbox.idempotency_key:
        raise SafetyDenied("OUTBOX_LOGICAL_EFFECT_ID_MISMATCH")
    if lease.logical_execution_id != intent.idempotency_key:
        raise SafetyDenied("INTENT_LOGICAL_EXECUTION_ID_MISMATCH")
    request = DispatchSafetyInput(
        tenant_id=interaction.tenant_id,
        lease_id=lease.id,
        budget_id=budget.id,
        consumption_id=consumption.id,
        logical_execution_id=intent.idempotency_key,
        logical_effect_id=outbox.idempotency_key,
        scenario_run_id=budget.scenario_run_id,
        effect_type="WHATSAPP_TEXT",
        direction=EffectDirection.SYSTEM,
        target_scope=recipient.reference,
        global_gate_open=settings.external_delivery_enabled,
        policy_allowed=gate == "ALLOWED",
        readiness_state=_readiness_state_for_scenario(
            session,
            scenario,
            now=timestamp,
        ),
        transport_ready=transport_ready,
    )
    validate_dispatch_and_bind_outbox_in_transaction(
        session,
        request,
        execution_intent_id=intent.id,
        outbox_message_id=outbox.id,
        now=timestamp,
    )


@traced_span("outbox.enqueue")
def enqueue_ready_intents(
    session: Session,
    limit: int = 10,
    *,
    transport_ready: bool = False,
    execution_intent_id: str | None = None,
) -> int:
    query = select(AgentExecutionIntentRow).where(
        AgentExecutionIntentRow.status == "READY", AgentExecutionIntentRow.release_status == "RELEASED"
    )
    if execution_intent_id:
        query = query.where(AgentExecutionIntentRow.id == execution_intent_id)
    intents = session.scalars(
        query
        # A frozen automatic backlog must not starve explicit human approval.
        .order_by(case((AgentExecutionIntentRow.authorization_source == "POLICY_AUTONOMY", 1),
                       else_=0), AgentExecutionIntentRow.created_at).limit(limit)
    ).all()
    count = 0
    for intent in intents:
        decision = session.get(AgentDecisionRow, intent.agent_decision_id)
        if decision and not grace_allows_interaction(session, decision.interaction_id, lock=True):
            intent.status = "CANCELLED"
            intent.blocked_reason = "CANCELED_BY_HUMAN_REPLY"
            audit(
                session,
                decision.interaction_id,
                "execution.cancelled",
                {"intent_id": intent.id, "reason": "CANCELED_BY_HUMAN_REPLY"},
                origin="owner_reply_grace",
            )
            continue
        gate, reason, recipient = execution_gate(
            session,
            intent,
            transport_ready=transport_ready,
        )
        if gate != "ALLOWED":
            # Pause freezes eligibility, not the durable item. No special replay
            # on resume: the ordinary gates above must pass again.
            if not reason.startswith("OWNER_AUTOMATION_"):
                intent.status = "BLOCKED"
            intent.blocked_reason = reason
            audit(session, decision.interaction_id, "execution.blocked", {"intent_id": intent.id, "reason": reason})
            continue
        if not settings.external_delivery_enabled:
            from attention_router.application.lab_conversation import consume_permit, permit_for_intent
            permit = permit_for_intent(session, intent)
            if permit is None:
                intent.status = "BLOCKED"
                intent.blocked_reason = "LAB_DELIVERY_PERMIT_REQUIRED"
                continue
            try:
                consume_permit(session, permit, intent)
            except ValueError:
                intent.status = "BLOCKED"
                intent.blocked_reason = "LAB_DELIVERY_PERMIT_WRONG_INTENT"
                continue
        existing = session.scalar(select(OutboxMessageRow).where(OutboxMessageRow.execution_intent_id == intent.id))
        if existing:
            if requires_platform_execution_safety(session, intent):
                try:
                    validate_platform_outbox_safety(
                        session,
                        intent=intent,
                        outbox=existing,
                        transport_ready=transport_ready,
                    )
                except SafetyDenied as exc:
                    intent.status = "BLOCKED"
                    intent.blocked_reason = exc.reason_code
                    continue
            intent.status = "QUEUED"
            continue
        interaction = session.get(InteractionRow, decision.interaction_id)
        event = session.get(InboundEventRow, decision.event_id)
        repetition = suppress_if_repeated(session, decision, interaction, event)
        if repetition.suppress:
            intent.status = "CANCELLED"
            intent.blocked_reason = repetition.reason
            continue
        response = (
            session.get(AgentResponseReviewRow, intent.response_review_id).effective_response
            if intent.response_review_id
            else intent.effective_response_snapshot
        )
        from attention_router.application.voice_tts import ensure_tts_derivation, intent_is_voice_response
        if intent_is_voice_response(session, intent):
            ensure_tts_derivation(session, intent)
            intent.status = "TTS_PENDING"
            intent.blocked_reason = None
            audit(session, interaction.id, "tts.derivation_enqueued", {"intent_id": intent.id})
            count += 1
            continue
        stamp = _now()
        outbox = OutboxMessageRow(
            id=new_id(), interaction_id=interaction.id, action_type="agent_execution_text", destination="local_transport",
            payload={"external_actor_id": recipient.reference, "message_type": "text", "text": response, "execution_intent_id": intent.id},
            status="PENDING", created_at=stamp, available_at=stamp, attempt_count=0,
            idempotency_key=f"execution:{intent.id}", correlation_id=interaction.correlation_id,
            causation_id=decision.id, execution_intent_id=intent.id,
        )
        if requires_platform_execution_safety(session, intent):
            try:
                with session.begin_nested():
                    session.add(outbox)
                    session.flush()
                    validate_platform_outbox_safety(
                        session,
                        intent=intent,
                        outbox=outbox,
                        transport_ready=transport_ready,
                    )
            except SafetyDenied as exc:
                intent.status = "BLOCKED"
                intent.blocked_reason = exc.reason_code
                audit(
                    session,
                    interaction.id,
                    "execution.blocked",
                    {"intent_id": intent.id, "reason": exc.reason_code},
                )
                continue
        else:
            session.add(outbox)
        intent.status = "QUEUED"
        intent.blocked_reason = None
        audit(session, interaction.id, "execution.outbox_enqueued", {"intent_id": intent.id, "outbox_id": outbox.id})
        count += 1
    session.flush()
    safe_set_attribute(current_span(), "attention.outbox_count", count)
    set_outcome(current_span(), "ENQUEUED" if count else "NO_OUTBOX")
    return count


def cancel_intent(session: Session, intent_id: str) -> AgentExecutionIntentRow:
    intent = session.get(AgentExecutionIntentRow, intent_id)
    if intent is None:
        raise ExecutionNotFound("execution intent not found")
    if intent.status in {"SENT", "CANCELLED"}:
        return intent
    intent.status = "CANCELLED"
    decision = session.get(AgentDecisionRow, intent.agent_decision_id)
    audit(session, decision.interaction_id, "execution.cancelled", {"intent_id": intent.id})
    return intent


def intent_to_dict(session: Session, intent: AgentExecutionIntentRow) -> dict[str, Any]:
    recipient = None
    try:
        recipient = resolve_recipient(session, intent).masked
    except ExecutionError:
        pass
    return {
        "id": intent.id, "agent_decision_id": intent.agent_decision_id, "response_review_id": intent.response_review_id,
        "autonomy_evaluation_id": intent.autonomy_evaluation_id, "authorization_source": intent.authorization_source,
        "intent_type": intent.intent_type, "status": intent.status, "release_status": intent.release_status,
        "released_at": intent.released_at.isoformat() if intent.released_at else None, "released_by": intent.released_by,
        "recipient_masked": recipient, "execution_allowed": intent.execution_allowed,
        "external_delivery_allowed": intent.external_delivery_allowed, "blocked_reason": intent.blocked_reason,
        "idempotency_key": intent.idempotency_key, "created_at": intent.created_at.isoformat(),
    }
