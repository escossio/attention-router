"""Explicit one-effect production execution after human approval STOP."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    BoundedRunAuthorizationRow,
    EffectBudgetRow,
    ExecutionIntentRow,
    ExecutionLeaseRow,
    HumanExecutionAuthorizationRow,
    InteractionRow,
    MetaDeliveryReconciliationRow,
    OutboxMessageRow,
    ScenarioRunRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.bounded_authorization import (
    check_bounded_authorization,
    create_bounded_authorization,
)
from attention_router.platform.execution_safety import (
    ClaimReservationRequest,
    EffectDirection,
    SafetyDenied,
    activate_scenario_run_for_execution,
    bind_reserved_consumption_to_execution_intent_in_transaction,
    bind_reserved_consumption_to_outbox_message_in_transaction,
    claim_and_reserve_in_transaction,
    provision_production_execution_safety_in_transaction,
)
from attention_router.platform.meta_callback_reconciliation import (
    consume_meta_api_acceptance,
    reconcile_meta_callback_outcome_in_transaction,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import (
    create_production_scenario_run,
    validate_materialized_execution_intent_authority,
)
from attention_router.platform.scenarios import ScenarioRunStatus, transition_scenario_run
from attention_router.platform.transaction_locks import acquire_meta_attempt_gate


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:48]}"


@dataclass(frozen=True, slots=True)
class ProductionExecutionPreparation:
    scenario_run_id: str
    effect_budget_id: str
    execution_lease_id: str
    bounded_authorization_id: str


@dataclass(frozen=True, slots=True)
class ProductionDispatchReservation:
    scenario_run_id: str
    effect_budget_id: str
    execution_lease_id: str
    consumption_id: str
    outbox_message_id: str


def _approved_graph(
    session: Session,
    *,
    agent_execution_intent_id: str,
    authorization_id: str,
) -> tuple[
    AgentExecutionIntentRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    InteractionRow,
]:
    agent = session.execute(
        select(AgentExecutionIntentRow)
        .where(AgentExecutionIntentRow.id == agent_execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        agent is None
        or agent.authorization_source != "PRODUCTION_EXECUTION_INTENT"
        or not agent.execution_intent_id
        or not agent.execution_intent_fingerprint
    ):
        raise ProductionAuthorityDenied("PRODUCTION_AGENT_PARENT_REQUIRED")
    parent = session.get(ExecutionIntentRow, agent.execution_intent_id)
    authorization = session.get(HumanExecutionAuthorizationRow, authorization_id)
    decision = session.get(AgentDecisionRow, agent.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    if (
        parent is None
        or parent.state != "MATERIALIZED"
        or parent.scope_fingerprint != agent.execution_intent_fingerprint
        or authorization is None
        or authorization.execution_intent_id != parent.id
        or authorization.execution_intent_fingerprint != parent.scope_fingerprint
        or authorization.state != "APPROVED"
        or authorization.decision_at is None
        or _utc(authorization.decision_at) >= _utc(authorization.expires_at)
        or interaction is None
        or interaction.tenant_id != parent.scope["target"]["tenant"]
    ):
        raise ProductionAuthorityDenied("PRODUCTION_APPROVED_GRAPH_INVALID")
    return agent, parent, authorization, interaction


def prepare_production_controlled_execution(
    session: Session,
    *,
    agent_execution_intent_id: str,
    authorization_id: str,
    run_id: str,
    root_correlation_id: str,
    lease_ttl_seconds: int = 600,
    now: datetime | None = None,
) -> ProductionExecutionPreparation:
    """Create one inert run, budget, lease and exact BRA, then arm the run."""

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    agent, parent, authorization, interaction = _approved_graph(
        session,
        agent_execution_intent_id=agent_execution_intent_id,
        authorization_id=authorization_id,
    )
    if parent.expires_at is None or timestamp >= _utc(parent.expires_at):
        raise ProductionAuthorityDenied("EXECUTION_INTENT_EXPIRED")
    validate_materialized_execution_intent_authority(
        session,
        parent=parent,
        expected_fingerprint=parent.scope_fingerprint,
        effective_response_snapshot=agent.effective_response_snapshot,
        now=timestamp,
    )
    run_expiry = min(_utc(parent.expires_at), timestamp + timedelta(seconds=lease_ttl_seconds))
    run = create_production_scenario_run(
        session,
        agent_execution_intent_id=agent.id,
        tenant_id=interaction.tenant_id,
        scenario_version_id=parent.scope["scenario"]["id"],
        run_id=run_id,
        root_correlation_id=root_correlation_id,
        expires_at=run_expiry,
        now=timestamp,
    )
    safety = provision_production_execution_safety_in_transaction(
        session,
        scenario_run_id=run.id,
        target_scope=agent.recipient_reference or "",
        valid_until=run_expiry,
        lease_ttl_seconds=lease_ttl_seconds,
        provenance={
            "execution_intent_id": parent.id,
            "execution_intent_fingerprint": parent.scope_fingerprint,
            "human_approval_record_id": authorization.id,
            "max_external_effects": 1,
            "retries": 0,
        },
        now=timestamp,
    )
    bounded = session.scalar(
        select(BoundedRunAuthorizationRow).where(
            BoundedRunAuthorizationRow.tenant_id == interaction.tenant_id,
            BoundedRunAuthorizationRow.scenario_run_id == run.id,
            BoundedRunAuthorizationRow.effect_budget_id == safety.system_budget_id,
        )
    )
    scopes = {
        "level": "L2",
        "actor_scope": f"human-approval:{authorization.id}",
        "target_scope": agent.recipient_reference or "",
        "capability_scope": "conversation.reply",
        "effect_scope": "WHATSAPP_TEXT",
    }
    if bounded is None:
        bounded = create_bounded_authorization(
            session,
            tenant_id=interaction.tenant_id,
            scenario_run_id=run.id,
            effect_budget_id=safety.system_budget_id,
            **scopes,
            max_effects=1,
            authorized_by="HUMAN_EXECUTION_AUTHORIZATION",
            correlation_id=root_correlation_id,
            valid_from=timestamp,
            expires_at=run_expiry,
            provenance={
                "human_approval_record_id": authorization.id,
                "execution_intent_id": parent.id,
                "execution_intent_fingerprint": parent.scope_fingerprint,
            },
            now=timestamp,
        )
    elif any(getattr(bounded, key) != value for key, value in scopes.items()):
        raise SafetyDenied("PRODUCTION_BOUNDED_AUTHORIZATION_DRIFT")
    activate_scenario_run_for_execution(
        session,
        scenario_run_id=run.id,
        authority=f"HUMAN_EXECUTION_AUTHORIZATION:{authorization.id}",
        requires_bounded_authorization=True,
        **scopes,
        now=timestamp,
    )
    audit(
        session,
        interaction.id,
        "production_controlled_execution_armed",
        {
            "execution_intent_id": parent.id,
            "agent_execution_intent_id": agent.id,
            "scenario_run_id": run.id,
            "effect_budget_id": safety.system_budget_id,
            "execution_lease_id": safety.system_lease_id,
            "bounded_authorization_id": bounded.id,
            "external_effect_limit": 1,
            "retry_limit": 0,
        },
        correlation_id=root_correlation_id,
        causation_id=authorization.id,
        tenant_id=interaction.tenant_id,
    )
    return ProductionExecutionPreparation(
        scenario_run_id=run.id,
        effect_budget_id=safety.system_budget_id,
        execution_lease_id=safety.system_lease_id,
        bounded_authorization_id=bounded.id,
    )


def reserve_production_conversation_reply(
    session: Session,
    *,
    agent_execution_intent_id: str,
    authorization_id: str,
    dispatcher_id: str,
    now: datetime | None = None,
) -> ProductionDispatchReservation:
    """Claim the one lease and durably isolate one Meta outbox attempt."""

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    logical_effect_id = f"execution:{agent_execution_intent_id}"
    expected_outbox_id = _stable_id("metaout", logical_effect_id)
    acquire_meta_attempt_gate(session, expected_outbox_id)
    existing_outbox = session.scalar(
        select(OutboxMessageRow)
        .where(OutboxMessageRow.id == expected_outbox_id)
        .with_for_update()
    )
    agent, parent, authorization, interaction = _approved_graph(
        session,
        agent_execution_intent_id=agent_execution_intent_id,
        authorization_id=authorization_id,
    )
    run = session.scalar(select(ScenarioRunRow).where(
        ScenarioRunRow.agent_execution_intent_id == agent.id
    ).with_for_update())
    if run is None or run.status != "ARMED" or timestamp >= _utc(run.expires_at):
        raise SafetyDenied("PRODUCTION_RUN_NOT_ARMED")
    budget = session.get(EffectBudgetRow, run.effect_budget_id)
    lease = session.scalar(select(ExecutionLeaseRow).where(
        ExecutionLeaseRow.scenario_run_id == run.id,
        ExecutionLeaseRow.effect_budget_id == budget.id,
    )) if budget else None
    bounded = session.scalar(select(BoundedRunAuthorizationRow).where(
        BoundedRunAuthorizationRow.scenario_run_id == run.id,
        BoundedRunAuthorizationRow.effect_budget_id == budget.id,
    )) if budget else None
    if budget is None or lease is None or bounded is None:
        raise SafetyDenied("PRODUCTION_EXECUTION_SAFETY_MISSING")
    scopes = {
        "level": "L2",
        "actor_scope": f"human-approval:{authorization.id}",
        "target_scope": agent.recipient_reference or "",
        "capability_scope": "conversation.reply",
        "effect_scope": "WHATSAPP_TEXT",
    }
    auth_result = check_bounded_authorization(
        session,
        tenant_id=interaction.tenant_id,
        scenario_run_id=run.id,
        effect_budget_id=budget.id,
        **scopes,
        now=timestamp,
    )
    if not auth_result.allowed:
        raise SafetyDenied(auth_result.reason_code)
    if agent.id != agent_execution_intent_id:
        raise SafetyDenied("PRODUCTION_AGENT_IDENTITY_MISMATCH")
    if existing_outbox is not None:
        raise SafetyDenied("PRODUCTION_OUTBOX_ALREADY_EXISTS_NO_RETRY")
    reservation = claim_and_reserve_in_transaction(
        session,
        ClaimReservationRequest(
            tenant_id=interaction.tenant_id,
            lease_id=lease.id,
            budget_id=budget.id,
            claimant_id=agent.id,
            claimed_event_id=None,
            logical_execution_id=agent.idempotency_key,
            lease_idempotency_key=lease.idempotency_key,
            logical_effect_id=logical_effect_id,
            effect_idempotency_key=logical_effect_id,
            effect_type="WHATSAPP_TEXT",
            direction=EffectDirection.SYSTEM,
            target_scope=agent.recipient_reference or "",
            scenario_run_id=run.id,
            provenance={
                "execution_intent_id": parent.id,
                "execution_intent_fingerprint": parent.scope_fingerprint,
                "human_approval_record_id": authorization.id,
                "bounded_admission_id": bounded.id,
                "retry_limit": 0,
            },
        ),
        now=timestamp,
    )
    bind_reserved_consumption_to_execution_intent_in_transaction(
        session,
        tenant_id=interaction.tenant_id,
        consumption_id=reservation.consumption_id,
        logical_effect_id=logical_effect_id,
        execution_intent_id=agent.id,
    )
    outbox = OutboxMessageRow(
        id=expected_outbox_id,
        interaction_id=interaction.id,
        action_type="production_conversation_reply",
        destination="meta_whatsapp_cloud",
        payload={
            "external_actor_id": agent.recipient_reference,
            "message_type": "text",
            "text": agent.effective_response_snapshot,
            "execution_intent_id": agent.id,
            "production_execution_intent_id": parent.id,
            "retry_policy": "NONE",
        },
        status="PROCESSING",
        created_at=timestamp,
        available_at=timestamp,
        claimed_at=timestamp,
        claimed_by=dispatcher_id,
        attempt_count=1,
        idempotency_key=logical_effect_id,
        correlation_id=run.root_correlation_id,
        causation_id=authorization.id,
        execution_intent_id=agent.id,
    )
    session.add(outbox)
    session.flush()
    bind_reserved_consumption_to_outbox_message_in_transaction(
        session,
        tenant_id=interaction.tenant_id,
        consumption_id=reservation.consumption_id,
        logical_effect_id=logical_effect_id,
        execution_intent_id=agent.id,
        outbox_message_id=outbox.id,
    )
    transition_scenario_run(run, ScenarioRunStatus.RUNNING, now=timestamp)
    agent.status = "QUEUED"
    agent.blocked_reason = None
    audit(
        session,
        interaction.id,
        "production_meta_dispatch_reserved",
        {
            "scenario_run_id": run.id,
            "outbox_id": outbox.id,
            "consumption_id": reservation.consumption_id,
            "attempt_count": 1,
            "retry_policy": "NONE",
        },
        correlation_id=run.root_correlation_id,
        causation_id=authorization.id,
        tenant_id=interaction.tenant_id,
    )
    return ProductionDispatchReservation(
        scenario_run_id=run.id,
        effect_budget_id=budget.id,
        execution_lease_id=lease.id,
        consumption_id=reservation.consumption_id,
        outbox_message_id=outbox.id,
    )


def mark_production_meta_api_accepted(
    session: Session,
    *,
    outbox_message_id: str,
    dispatcher_id: str,
    provider_message_id: str,
    now: datetime | None = None,
) -> None:
    """Consume the sole authority and open normalized result reconciliation."""

    consume_meta_api_acceptance(
        session,
        outbox_message_id=outbox_message_id,
        dispatcher_id=dispatcher_id,
        provider_message_id=provider_message_id,
        now=now or datetime.now(UTC),
        admission_grace_seconds=settings.meta_admission_grace_seconds,
    )


def finalize_delivered_production_conversation_reply(
    session: Session,
    *,
    outbox_message_id: str,
    now: datetime | None = None,
) -> None:
    """Compatibility name routed through normalized V2 evidence only."""

    reconciliation = session.scalar(
        select(MetaDeliveryReconciliationRow).where(
            MetaDeliveryReconciliationRow.outbox_message_id == outbox_message_id
        )
    )
    if reconciliation is None:
        raise SafetyDenied("PRODUCTION_META_RECONCILIATION_MISSING")
    result = reconcile_meta_callback_outcome_in_transaction(
        session,
        reconciliation_id=reconciliation.id,
        now=now,
    )
    if result.decision != "PASSED":
        raise SafetyDenied("PRODUCTION_META_DELIVERY_NOT_PROVED")


__all__ = [
    "ProductionDispatchReservation",
    "ProductionExecutionPreparation",
    "finalize_delivered_production_conversation_reply",
    "mark_production_meta_api_accepted",
    "prepare_production_controlled_execution",
    "reserve_production_conversation_reply",
]
