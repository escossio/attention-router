from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.application.direct_conversation import (
    DIRECT_TEXT_ACTIONS, DIRECT_TEXT_CONVERSATION_CONTRACT_VERSION,
    direct_conversation_eligibility, conversational_policy_config,
)
from attention_router.application.owner_automation_control import automation_denial_reason
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    AgentBlueprintVersionRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    AutonomyEvaluationRow,
    InboundEventRow,
    InteractionRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.application.repetition import suppress_if_repeated
from attention_router.observability.tracing import current_span, safe_set_attribute, set_outcome, start_span, traced_span
from attention_router.platform.execution_safety import (
    reserve_synthetic_system_effect_for_intent_in_transaction,
)


OBSERVE = "OBSERVE"
REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
AUTO_ALLOWED = "AUTO_ALLOWED"
_MODE_RANK = {OBSERVE: 0, REQUIRES_APPROVAL: 1, AUTO_ALLOWED: 2}


@dataclass(frozen=True)
class AutonomyResult:
    blueprint_mode: str
    policy_mode: str
    effective_mode: str
    action_allowed: bool
    actor_scope_valid: bool
    audience_scope_valid: bool
    freshness_valid: bool
    from_me: bool
    automatic_execution_allowed: bool
    reason_code: str


def _mode(value: Any) -> str:
    value = str(value or "").upper()
    aliases = {
        "OBSERVE": OBSERVE,
        "SUGGEST": REQUIRES_APPROVAL,
        "APPROVAL_REQUIRED": REQUIRES_APPROVAL,
        "REQUIRES_APPROVAL": REQUIRES_APPROVAL,
        "LIMITED_AUTONOMY": AUTO_ALLOWED,
        "AUTONOMOUS": AUTO_ALLOWED,
        "AUTO_ALLOWED": AUTO_ALLOWED,
    }
    return aliases.get(value, OBSERVE)


def blueprint_mode(version: AgentBlueprintVersionRow | None) -> str:
    spec = version.spec if version else {}
    autonomy = spec.get("autonomy") or {}
    return _mode(autonomy.get("execution_mode") or autonomy.get("mode") or autonomy.get("level"))


def policy_mode(policy_config: dict[str, Any] | None) -> str:
    return _mode((policy_config or {}).get("execution_mode"))


def _scope_match(scope: dict[str, Any], decision: AgentDecisionRow, interaction: InteractionRow) -> tuple[bool, bool]:
    audiences = scope.get("audiences")
    actor_ids = scope.get("actor_ids")
    audience_ok = bool(audiences) and decision.audience in audiences
    actor_ok = not actor_ids or decision.actor_id in actor_ids
    return actor_ok, audience_ok


def _fresh(event: InboundEventRow) -> bool:
    if not settings.autonomous_execution_activated_at:
        return False
    try:
        watermark = datetime.fromisoformat(settings.autonomous_execution_activated_at.replace("Z", "+00:00"))
        if watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    received = event.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - received).total_seconds()
    return received >= watermark and 0 <= age <= settings.autonomous_decision_max_age_seconds


def evaluate(
    decision: AgentDecisionRow,
    event: InboundEventRow,
    interaction: InteractionRow,
    blueprint: AgentBlueprintVersionRow | None,
    policy_config: dict[str, Any] | None,
) -> AutonomyResult:
    selected_policy_present = policy_config is not None
    direct = direct_conversation_eligibility(event)
    text_conversation = direct.eligible and decision.recommended_action in DIRECT_TEXT_ACTIONS
    if text_conversation:
        policy_config = conversational_policy_config(policy_config)
    b_mode = blueprint_mode(blueprint)
    p_mode = policy_mode(policy_config)
    effective = min((b_mode, p_mode), key=lambda value: _MODE_RANK[value])
    config = policy_config or {}
    allowed_actions = config.get("allowed_actions") or []
    action_allowed = decision.recommended_action in allowed_actions
    scope = config.get("execution_scope") or {}
    actor_ok, audience_ok = _scope_match(scope, decision, interaction)
    if text_conversation and not scope:
        actor_ok, audience_ok = True, True
    payload = event.payload or {}
    metadata = payload.get("metadata") or {}
    from_me = bool(
        payload.get("from_me")
        or payload.get("fromMe")
        or metadata.get("from_me")
        or metadata.get("fromMe")
    )
    fresh = _fresh(event)
    if text_conversation and not selected_policy_present:
        return AutonomyResult(b_mode, p_mode, effective, False, actor_ok,
                              audience_ok, fresh, from_me, False, "POLICY_VERSION_MISSING")
    if event.source == "wwebjs" and not direct.eligible:
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok,
                              audience_ok, fresh, from_me, False, "SELF_MESSAGE" if from_me else direct.reason_code)
    if (event.source == "wwebjs" or payload.get("channel") == "whatsapp") and not (decision.proposed_response or "").strip():
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok,
                              audience_ok, fresh, from_me, False, "AUTOMATIC_RESPONSE_EMPTY")
    if effective != AUTO_ALLOWED:
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok, audience_ok, fresh, from_me, False, "REVIEW_REQUIRED" if effective == REQUIRES_APPROVAL else "OBSERVE_ONLY")
    if not settings.autonomous_execution_enabled:
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok, audience_ok, fresh, from_me, False, "AUTONOMOUS_EXECUTION_DISABLED")
    if not settings.external_delivery_enabled:
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok, audience_ok, fresh, from_me, False, "EXTERNAL_DELIVERY_DISABLED")
    if from_me:
        return AutonomyResult(b_mode, p_mode, effective, action_allowed, actor_ok, audience_ok, fresh, from_me, False, "SELF_MESSAGE")
    if not action_allowed:
        return AutonomyResult(b_mode, p_mode, effective, False, actor_ok, audience_ok, fresh, from_me, False, "ACTION_NOT_ALLOWED")
    if not actor_ok or not audience_ok:
        return AutonomyResult(b_mode, p_mode, effective, True, actor_ok, audience_ok, fresh, from_me, False, "SCOPE_NOT_ALLOWED")
    if not fresh:
        return AutonomyResult(b_mode, p_mode, effective, True, True, True, False, from_me, False, "STALE_OR_BEFORE_ACTIVATION")
    return AutonomyResult(b_mode, p_mode, effective, True, True, True, True, False, True, "AUTO_ALLOWED")


def evaluation_to_dict(row: AutonomyEvaluationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_decision_id": row.agent_decision_id,
        "event_id": row.event_id,
        "interaction_id": row.interaction_id,
        "agent_blueprint_id": row.agent_blueprint_id,
        "agent_blueprint_version": row.agent_blueprint_version,
        "policy_version_id": row.policy_version_id,
        "blueprint_mode": row.blueprint_mode,
        "policy_mode": row.policy_mode,
        "effective_mode": row.effective_mode,
        "decision_type": row.decision_type,
        "recommended_action": row.recommended_action,
        "action_allowed": row.action_allowed,
        "actor_scope_valid": row.actor_scope_valid,
        "audience_scope_valid": row.audience_scope_valid,
        "freshness_valid": row.freshness_valid,
        "from_me": row.from_me,
        "automatic_execution_allowed": row.automatic_execution_allowed,
        "reason_code": row.reason_code,
        "created_at": row.created_at.isoformat(),
    }


@traced_span("autonomy.evaluate")
def evaluate_and_route(session: Session, decision: AgentDecisionRow) -> AutonomyEvaluationRow:
    existing = session.scalar(select(AutonomyEvaluationRow).where(AutonomyEvaluationRow.agent_decision_id == decision.id))
    if existing:
        return existing
    event = session.get(InboundEventRow, decision.event_id)
    interaction = session.get(InteractionRow, decision.interaction_id)
    blueprint = None
    if decision.agent_blueprint_id and decision.agent_blueprint_version:
        blueprint = session.scalar(
            select(AgentBlueprintVersionRow).where(
                AgentBlueprintVersionRow.blueprint_id == decision.agent_blueprint_id,
                AgentBlueprintVersionRow.version == decision.agent_blueprint_version,
            )
        )
    policy_config: dict[str, Any] | None = None
    if decision.policy_version_id:
        from attention_router.infrastructure.models import PolicyVersionRow
        policy = session.get(PolicyVersionRow, decision.policy_version_id)
        policy_config = policy.config if policy else None
    result = evaluate(decision, event, interaction, blueprint, policy_config)
    pause_reason = automation_denial_reason(session, interaction.tenant_id)
    if pause_reason:
        result = replace(result, automatic_execution_allowed=False, reason_code=pause_reason)
    safe_set_attribute(current_span(), "attention.policy_mode", result.policy_mode)
    safe_set_attribute(current_span(), "attention.effective_mode", result.effective_mode)
    safe_set_attribute(current_span(), "attention.action_allowed", result.action_allowed)
    safe_set_attribute(current_span(), "attention.automatic_execution_allowed", result.automatic_execution_allowed)
    row = AutonomyEvaluationRow(
        id=new_id(), agent_decision_id=decision.id, event_id=event.id, interaction_id=interaction.id,
        agent_blueprint_id=decision.agent_blueprint_id, agent_blueprint_version=decision.agent_blueprint_version,
        policy_version_id=decision.policy_version_id, blueprint_mode=result.blueprint_mode,
        policy_mode=result.policy_mode, effective_mode=result.effective_mode, decision_type=decision.decision_type,
        recommended_action=decision.recommended_action, action_allowed=result.action_allowed,
        actor_scope_valid=result.actor_scope_valid, audience_scope_valid=result.audience_scope_valid,
        freshness_valid=result.freshness_valid, from_me=result.from_me,
        automatic_execution_allowed=result.automatic_execution_allowed, reason_code=result.reason_code,
        created_at=now_utc(),
        conversation_contract_version=(DIRECT_TEXT_CONVERSATION_CONTRACT_VERSION
            if direct_conversation_eligibility(event).eligible
            and decision.recommended_action in DIRECT_TEXT_ACTIONS else None),
    )
    session.add(row)
    audit(session, interaction.id, "conversation.authorized", {
        "conversation_contract_version": row.conversation_contract_version,
        "conversation_eligible": direct_conversation_eligibility(event).eligible,
        "conversation_authorization": result.reason_code,
        "selected_policy_id": policy.policy_id if decision.policy_version_id and policy else None,
    }, origin="autonomy")
    # Persist the evaluation before creating the FK-dependent execution intent.
    session.flush()
    audit(session, interaction.id, "autonomy.evaluation_started", {"decision_id": decision.id}, origin="autonomy")
    audit(session, interaction.id, "autonomy.evaluated", {"evaluation_id": row.id, "effective_mode": result.effective_mode, "reason": result.reason_code}, origin="autonomy")
    repetition = None
    if decision.proposed_response:
        with start_span("repetition_guard.evaluate") as repetition_span:
            repetition = suppress_if_repeated(session, decision, interaction, event)
            safe_set_attribute(repetition_span, "attention.repetition.semantic_repeat", repetition.suppress)
            safe_set_attribute(repetition_span, "attention.repetition.state_changed", not repetition.suppress)
            safe_set_attribute(repetition_span, "attention.repetition.suppressed", repetition.suppress)
            safe_set_attribute(repetition_span, "attention.repetition.reason", repetition.reason)
            safe_set_attribute(
                repetition_span,
                "attention.response_source",
                "OPENAI_AGENTS_SDK" if settings.andy_agent_enabled else "ANDY_BEHAVIOR",
            )
            safe_set_attribute(repetition_span, "attention.repetition.response_objective", repetition.response_objective)
            safe_set_attribute(repetition_span, "attention.repetition.objective_already_satisfied", repetition.objective_already_satisfied)
            safe_set_attribute(repetition_span, "attention.repetition.previous_useful_response_generated", repetition.previous_useful_response_generated)
            safe_set_attribute(repetition_span, "attention.repetition.previous_useful_response_delivered", repetition.previous_useful_response_delivered)
            safe_set_attribute(repetition_span, "attention.repetition.suppression_reason", repetition.reason)
            set_outcome(repetition_span, "SUPPRESSED" if repetition.suppress else "ALLOWED")
    if pause_reason:
        audit(session, interaction.id, "autonomy.blocked",
              {"evaluation_id": row.id, "reason": pause_reason}, origin="autonomy")
    elif repetition and repetition.suppress:
        row.automatic_execution_allowed = False
        row.reason_code = repetition.reason
        audit(session, interaction.id, "autonomy.blocked", {"evaluation_id": row.id, "reason": row.reason_code}, origin="autonomy")
    elif result.effective_mode == OBSERVE:
        audit(session, interaction.id, "autonomy.observe", {"evaluation_id": row.id}, origin="autonomy")
    elif result.effective_mode == REQUIRES_APPROVAL:
        audit(session, interaction.id, "autonomy.review_required", {"evaluation_id": row.id}, origin="autonomy")
        if settings.agent_response_review_enabled and decision.proposed_response:
            from attention_router.application.response_review import create_review_for_decision
            create_review_for_decision(session, decision.id)
    elif result.automatic_execution_allowed:
        audit(session, interaction.id, "autonomy.auto_allowed", {"evaluation_id": row.id}, origin="autonomy")
        if _create_autonomous_intent(session, decision, row) is not None:
            audit(session, interaction.id, "autonomy.execution_intent_created", {"evaluation_id": row.id}, origin="autonomy")
            audit(session, interaction.id, "autonomy.execution_released", {"evaluation_id": row.id}, origin="autonomy")
    else:
        audit(session, interaction.id, "autonomy.blocked", {"evaluation_id": row.id, "reason": result.reason_code}, origin="autonomy")
    session.flush()
    set_outcome(current_span(), "ALLOWED" if row.automatic_execution_allowed else "BLOCKED")
    return row


@traced_span("execution.intent")
def _create_autonomous_intent(session: Session, decision: AgentDecisionRow, evaluation: AutonomyEvaluationRow) -> AgentExecutionIntentRow | None:
    interaction = session.get(InteractionRow, decision.interaction_id)
    reason = automation_denial_reason(session, interaction.tenant_id, lock=True)
    if reason:
        evaluation.automatic_execution_allowed = False
        evaluation.reason_code = reason
        audit(session, interaction.id, "autonomy.blocked",
              {"evaluation_id": evaluation.id, "reason": reason}, origin="autonomy")
        return None
    existing = session.scalar(select(AgentExecutionIntentRow).where(AgentExecutionIntentRow.autonomy_evaluation_id == evaluation.id))
    if existing:
        reserve_synthetic_system_effect_for_intent_in_transaction(
            session,
            intent=existing,
            readiness_max_age_seconds=settings.readiness_result_max_age,
        )
        return existing
    intent = AgentExecutionIntentRow(
        id=new_id(), agent_decision_id=decision.id, response_review_id=None, autonomy_evaluation_id=evaluation.id,
        authorization_source="POLICY_AUTONOMY", intent_type="WHATSAPP_RESPONSE",
        effective_response_snapshot=decision.proposed_response or "", status="READY", execution_allowed=True,
        external_delivery_allowed=True, blocked_reason=None, release_status="RELEASED",
        released_at=now_utc(), released_by=f"policy:{decision.policy_version_id}",
        idempotency_key=f"autonomy:{decision.id}:{decision.decision_pipeline_version}", created_at=now_utc(),
    )
    session.add(intent)
    session.flush()
    reserve_synthetic_system_effect_for_intent_in_transaction(
        session,
        intent=intent,
        readiness_max_age_seconds=settings.readiness_result_max_age,
    )
    safe_set_attribute(current_span(), "attention.intent_created", True)
    safe_set_attribute(current_span(), "attention.intent_status", intent.status)
    safe_set_attribute(current_span(), "attention.authorization_source", intent.authorization_source)
    set_outcome(current_span(), "CREATED")
    return intent
