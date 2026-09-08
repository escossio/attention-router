"""Transactional bridge from semantic production intent to the legacy ledger."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ExecutionClassVersionRow,
    ExecutionIntentRow,
    ExecutionIntentTargetBindingRow,
    HumanExecutionAuthorizationRow,
    InboundEventRow,
    InteractionRow,
    PolicyVersionRow,
    RecipientEndpointRow,
    SafetySetVersionRow,
    ScenarioRunRow,
    ScenarioVersionAuthorityBindingRow,
    ScenarioVersionRow,
    StaticIntentAuthorityProfileRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.production_authority import (
    FrozenAuthority,
    ProductionAuthorityDenied,
    frozen_authority_from_scope,
    semantic_scope_fingerprint,
    validate_not_expired,
    validate_production_scope_dependency_graph,
    validate_single_recipient,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_snapshot(snapshot: object, expected: dict[str, object], code: str) -> None:
    if not isinstance(snapshot, dict) or snapshot != expected:
        raise ProductionAuthorityDenied(code)


def _projection_request(parent: ExecutionIntentRow, authority: FrozenAuthority) -> dict:
    return {
        "scope_fingerprint": parent.scope_fingerprint,
        "transport": authority.transport,
        "operation": authority.operation,
        "capability": authority.capability,
        "audience": parent.scope["audience"],
        "limits": {
            "target_count": authority.target_count,
            "outbound_messages": authority.outbound_messages,
            "action_count": authority.action_count,
            "retries": authority.retries,
            "expires_at": authority.expires_at.astimezone(UTC).isoformat(),
            "target_identity": sorted(authority.target_identity),
        },
    }


def _validate_frozen_semantics(
    session: Session,
    *,
    parent: ExecutionIntentRow,
    effective_response_snapshot: str,
    timestamp: datetime,
) -> tuple[FrozenAuthority, RecipientEndpointRow]:
    scope = parent.scope
    authority = frozen_authority_from_scope(scope)
    if semantic_scope_fingerprint(scope) != parent.scope_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_SEMANTIC_DRIFT")
    if parent.expires_at is None or _utc(parent.expires_at) != _utc(authority.expires_at):
        raise ProductionAuthorityDenied("EXECUTION_INTENT_EXPIRY_DRIFT")
    validate_not_expired(authority.expires_at, timestamp)
    immutable_inputs = scope["immutable_inputs"]
    if immutable_inputs.get("effective_response_snapshot") != effective_response_snapshot:
        raise ProductionAuthorityDenied("IMMUTABLE_INPUT_MISMATCH")

    scenario_scope = scope["scenario"]
    class_scope = scope["execution_class"]
    safety_scope = scope["safety_set"]
    profile_scope = scope["authority_profile"]
    policy_scopes = scope["policies"]
    if not all(isinstance(value, dict) for value in (
        scenario_scope, class_scope, safety_scope, profile_scope,
    )) or not isinstance(policy_scopes, list) or len(policy_scopes) != 1:
        raise ProductionAuthorityDenied("SEMANTIC_COMPONENT_SCHEMA_MISMATCH")

    scenario = session.get(ScenarioVersionRow, scenario_scope.get("id"))
    execution_class = session.get(ExecutionClassVersionRow, class_scope.get("id"))
    safety_set = session.get(SafetySetVersionRow, safety_scope.get("id"))
    authority_profile = session.get(StaticIntentAuthorityProfileRow, profile_scope.get("id"))
    policy_scope = policy_scopes[0]
    policy = session.get(PolicyVersionRow, policy_scope.get("id")) if isinstance(policy_scope, dict) else None
    if any(value is None for value in (
        scenario, execution_class, safety_set, authority_profile, policy,
    )):
        raise ProductionAuthorityDenied("SEMANTIC_AUTHORITY_DEPENDENCY_MISSING")

    _require_snapshot(scenario_scope, {
        "id": scenario.id, "version": scenario.version, "content_hash": scenario.content_hash,
    }, "SCENARIO_VERSION_DRIFT")
    _require_snapshot(class_scope, {
        "id": execution_class.id, "identity": execution_class.identity,
        "version": execution_class.version,
    }, "EXECUTION_CLASS_DRIFT")
    _require_snapshot(safety_scope, {
        "id": safety_set.id, "identity": safety_set.identity,
        "version": safety_set.version,
    }, "SAFETY_SET_DRIFT")
    _require_snapshot(profile_scope, {
        "id": authority_profile.id, "identity": authority_profile.identity,
        "version": authority_profile.version,
    }, "AUTHORITY_PROFILE_DRIFT")
    _require_snapshot(policy_scope, {
        "id": policy.id, "policy_id": policy.policy_id, "version": policy.version,
        "checksum": policy.checksum,
    }, "POLICY_VERSION_DRIFT")
    if parent.authority_profile_id != authority_profile.id:
        raise ProductionAuthorityDenied("AUTHORITY_PROFILE_BINDING_MISMATCH")

    bindings = session.scalars(select(ScenarioVersionAuthorityBindingRow).where(
        ScenarioVersionAuthorityBindingRow.scenario_version_id == scenario.id
    )).all()
    binding_ids = {
        row.binding_role: row.execution_class_version_id or row.safety_set_version_id
        or row.policy_version_id or row.authority_profile_id
        for row in bindings
    }
    if binding_ids != {
        "EXECUTION_CLASS": execution_class.id,
        "SAFETY_SET": safety_set.id,
        "POLICY": policy.id,
        "AUTHORITY_PROFILE": authority_profile.id,
    }:
        raise ProductionAuthorityDenied("SCENARIO_AUTHORITY_BINDING_DRIFT")

    targets = session.scalars(select(ExecutionIntentTargetBindingRow).where(
        ExecutionIntentTargetBindingRow.execution_intent_id == parent.id
    )).all()
    if len(targets) != 1:
        raise ProductionAuthorityDenied("EXACTLY_ONE_RECIPIENT_REQUIRED")
    endpoint = session.get(RecipientEndpointRow, targets[0].recipient_endpoint_id)
    if endpoint is None or endpoint.status != "ACTIVE":
        raise ProductionAuthorityDenied("RECIPIENT_ENDPOINT_INACTIVE_OR_MISSING")
    target_scope = scope["target"]
    _require_snapshot(target_scope, {
        "tenant": endpoint.tenant_id,
        "transport": endpoint.transport,
        "canonical_address": endpoint.canonical_address,
    }, "TARGET_AUTHORITY_DRIFT")
    validate_single_recipient(
        target_type=targets[0].target_type,
        target_count=len(targets),
        endpoint_transport=endpoint.transport,
        transport=authority.transport,
    )
    target_identity = (
        f"{endpoint.tenant_id}|{endpoint.transport}|{endpoint.canonical_address}",
    )
    if authority.target_identity != target_identity:
        raise ProductionAuthorityDenied("TARGET_IDENTITY_DRIFT")
    validate_production_scope_dependency_graph(
        scenario_version=scenario,
        execution_class=execution_class,
        safety_set=safety_set,
        policy=policy,
        authority_profile=authority_profile,
        transport=authority.transport,
        operation=authority.operation,
        capability=authority.capability,
        frozen_authority=authority,
        audience=scope["audience"],
        expires_at=authority.expires_at,
    )
    return authority, endpoint


def validate_frozen_execution_intent_authority(
    session: Session,
    *,
    parent: ExecutionIntentRow,
    expected_fingerprint: str,
    effective_response_snapshot: str,
    now: datetime | None = None,
) -> tuple[FrozenAuthority, RecipientEndpointRow]:
    """Revalidate the exact frozen production authority without materializing it."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if parent.state != "FROZEN":
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FROZEN")
    if not expected_fingerprint or parent.scope_fingerprint != expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if parent.expires_at is None:
        raise ProductionAuthorityDenied("EXPIRY_REQUIRED")
    immutable_inputs = parent.scope.get("immutable_inputs")
    if not isinstance(immutable_inputs, dict) or not isinstance(
        immutable_inputs.get("effective_response_snapshot"), str
    ):
        raise ProductionAuthorityDenied("IMMUTABLE_INPUT_MISMATCH")
    return _validate_frozen_semantics(
        session,
        parent=parent,
        effective_response_snapshot=effective_response_snapshot,
        timestamp=timestamp,
    )


def validate_materialized_execution_intent_authority(
    session: Session,
    *,
    parent: ExecutionIntentRow,
    expected_fingerprint: str,
    effective_response_snapshot: str,
    now: datetime | None = None,
) -> tuple[FrozenAuthority, RecipientEndpointRow]:
    """Freshly validate a materialized authority without reopening approval."""

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if parent.state != "MATERIALIZED":
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_MATERIALIZED")
    if not expected_fingerprint or parent.scope_fingerprint != expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    return _validate_frozen_semantics(
        session,
        parent=parent,
        effective_response_snapshot=effective_response_snapshot,
        timestamp=timestamp,
    )


def freeze_production_execution_intent(
    session: Session,
    *,
    parent: ExecutionIntentRow,
    expected_fingerprint: str,
    effective_response_snapshot: str,
    now: datetime | None = None,
) -> ExecutionIntentRow:
    """Freeze only a complete exact-one-target production authority graph."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if parent.state != "PREPARED":
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_PREPARED")
    if not expected_fingerprint or parent.scope_fingerprint != expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if parent.expires_at is None:
        raise ProductionAuthorityDenied("EXPIRY_REQUIRED")
    immutable_inputs = parent.scope.get("immutable_inputs")
    if not isinstance(immutable_inputs, dict) or immutable_inputs.get("effective_response_snapshot") != effective_response_snapshot:
        raise ProductionAuthorityDenied("IMMUTABLE_INPUT_MISMATCH")
    _validate_frozen_semantics(
        session,
        parent=parent,
        effective_response_snapshot=effective_response_snapshot,
        timestamp=timestamp,
    )
    parent.state, parent.frozen_at = "FROZEN", timestamp
    session.flush()
    return parent


def materialize_production_agent_intent(
    session: Session,
    *,
    execution_intent_id: str,
    expected_fingerprint: str,
    agent_decision_id: str,
    effective_response_snapshot: str,
    now: datetime | None = None,
) -> AgentExecutionIntentRow:
    """Create the one operational projection and mark its parent atomically.

    The caller owns the surrounding session transaction; no run, budget, lease,
    outbox, timer, or transport operation is performed here.
    """
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    parent = session.execute(
        select(ExecutionIntentRow).where(ExecutionIntentRow.id == execution_intent_id).with_for_update()
    ).scalar_one_or_none()
    if parent is None:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FOUND")
    if parent.state not in {"FROZEN", "MATERIALIZED"}:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FROZEN")
    if not expected_fingerprint or parent.scope_fingerprint != expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if parent.state == "FROZEN":
        authority, endpoint = validate_frozen_execution_intent_authority(
            session, parent=parent, expected_fingerprint=expected_fingerprint,
            effective_response_snapshot=effective_response_snapshot,
            now=timestamp,
        )
    else:
        if parent.expires_at is None:
            raise ProductionAuthorityDenied("EXPIRY_REQUIRED")
        validate_not_expired(parent.expires_at, timestamp)
        authority, endpoint = _validate_frozen_semantics(
            session, parent=parent,
            effective_response_snapshot=effective_response_snapshot,
            timestamp=timestamp,
        )
    approved = session.scalar(select(HumanExecutionAuthorizationRow).where(
        HumanExecutionAuthorizationRow.execution_intent_id == parent.id,
        HumanExecutionAuthorizationRow.state == "APPROVED",
        HumanExecutionAuthorizationRow.execution_intent_fingerprint == expected_fingerprint,
    ).limit(1))
    if approved is None or _utc(approved.expires_at) <= timestamp:
        raise ProductionAuthorityDenied("HEA_NOT_APPROVED")

    capability_request = _projection_request(parent, authority)

    existing = session.scalar(
        select(AgentExecutionIntentRow).where(
            AgentExecutionIntentRow.execution_intent_id == parent.id
        ).with_for_update()
    )
    if existing is not None:
        if (
            existing.execution_intent_fingerprint != expected_fingerprint
            or existing.effective_response_snapshot != effective_response_snapshot
            or existing.recipient_reference != endpoint.canonical_address
            or existing.capability_name != authority.capability
            or existing.capability_request != capability_request
        ):
            raise ProductionAuthorityDenied("PROJECTION_SEMANTIC_DRIFT")
        return existing
    if parent.state == "MATERIALIZED":
        raise ProductionAuthorityDenied("MATERIALIZED_PROJECTION_MISSING")

    projection = AgentExecutionIntentRow(
        id=str(uuid4()), agent_decision_id=agent_decision_id,
        authorization_source="PRODUCTION_EXECUTION_INTENT",
        intent_type="WHATSAPP_RESPONSE", effective_response_snapshot=effective_response_snapshot,
        status="BLOCKED", execution_allowed=False, external_delivery_allowed=False,
        blocked_reason="PRODUCTION_AUTHORITY_REQUIRES_DOWNSTREAM_ADMISSION",
        release_status="HELD", idempotency_key=f"production-execution-intent:{parent.id}",
        recipient_reference=endpoint.canonical_address,
        capability_name=authority.capability,
        capability_request=capability_request,
        execution_intent_id=parent.id, execution_intent_fingerprint=expected_fingerprint,
        created_at=timestamp,
    )
    session.add(projection)
    session.flush()
    result = session.execute(
        update(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == parent.id,
               ExecutionIntentRow.state == "FROZEN",
               ExecutionIntentRow.scope_fingerprint == expected_fingerprint)
        .values(state="MATERIALIZED")
    )
    if result.rowcount != 1:
        raise ProductionAuthorityDenied("MATERIALIZED_TRANSITION_CONFLICT")
    parent.state = "MATERIALIZED"
    return projection


def materialize_approved_production_agent_intent(
    session: Session,
    *,
    execution_intent_id: str,
    authorization_id: str,
    expected_fingerprint: str,
    effective_response_snapshot: str,
    now: datetime | None = None,
) -> AgentExecutionIntentRow:
    """Project real approved control-plane evidence into the legacy ledger.

    Meta ingress intentionally remains in shadow mode.  This seam creates only
    the minimum technical lineage required by the legacy execution ledger and
    derives it from the persisted, HMAC-gated button reply; it never fabricates
    or replays a webhook and performs no run, safety or transport work.
    """

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    parent = session.execute(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    if parent is None:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FOUND")
    authorization = session.execute(
        select(HumanExecutionAuthorizationRow)
        .where(HumanExecutionAuthorizationRow.id == authorization_id)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        authorization is None
        or authorization.execution_intent_id != parent.id
        or authorization.state != "APPROVED"
        or authorization.execution_intent_fingerprint != expected_fingerprint
        or not authorization.decision_inbound_wamid
        or not authorization.decision_sender
        or not authorization.decision_button_id
        or not authorization.request_wamid
    ):
        raise ProductionAuthorityDenied("HEA_APPROVAL_EVIDENCE_INCOMPLETE")
    if _utc(authorization.expires_at) <= timestamp:
        raise ProductionAuthorityDenied("HEA_NOT_APPROVED")

    normalized = session.scalar(
        select(InboundEventRow)
        .where(
            InboundEventRow.tenant_id == parent.scope["target"]["tenant"],
            InboundEventRow.source == "meta_whatsapp_shadow",
            InboundEventRow.external_event_id
            == authorization.decision_inbound_wamid,
        )
        .order_by(InboundEventRow.received_at.desc())
        .limit(1)
    )
    payload = normalized.payload if normalized is not None else {}
    if (
        normalized is None
        or payload.get("sender") != authorization.decision_sender
        or payload.get("message_type") != "interactive"
        or payload.get("interactive_type") != "button_reply"
        or payload.get("button_reply_id") != authorization.decision_button_id
        or payload.get("context_id") != authorization.request_wamid
        or payload.get("normalization") != "PASS"
        or payload.get("dispatch_enabled") is not False
    ):
        raise ProductionAuthorityDenied("REAL_META_APPROVAL_LINEAGE_MISSING")

    suffix = hashlib.sha256(authorization.id.encode()).hexdigest()[:40]
    interaction_id = f"prod-int-{suffix}"
    event_id = f"prod-evt-{suffix}"
    decision_id = f"prod-dec-{suffix}"
    interaction = session.get(InteractionRow, interaction_id)
    if interaction is None:
        interaction = InteractionRow(
            id=interaction_id,
            tenant_id=parent.scope["target"]["tenant"],
            event_type="production_human_approval",
            contact_id=authorization.decision_sender,
            contact_name="Production approver",
            relationship_category="authorized_operator",
            inbound_text="[persisted human approval control event]",
            state="closed",
            correlation_id=authorization.correlation_id,
            causation_id=authorization.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(interaction)
        session.flush()
    event = session.get(InboundEventRow, event_id)
    event_payload = {
        "authorization_id": authorization.id,
        "execution_intent_id": parent.id,
        "decision": "APPROVE",
        "source_inbound_event_id": normalized.id,
    }
    if event is None:
        event = InboundEventRow(
            id=event_id,
            tenant_id=interaction.tenant_id,
            source="production_human_approval",
            external_event_id=authorization.decision_inbound_wamid,
            event_type="approval",
            payload=event_payload,
            payload_hash=hashlib.sha256(
                json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            received_at=authorization.decision_at or timestamp,
            processed_at=timestamp,
            interaction_id=interaction.id,
            status="processed",
            correlation_id=authorization.correlation_id,
            lineage_classification="ORGANIC",
        )
        session.add(event)
        session.flush()
    decision = session.get(AgentDecisionRow, decision_id)
    if decision is None:
        decision = AgentDecisionRow(
            id=decision_id,
            event_id=event.id,
            interaction_id=interaction.id,
            actor_id=authorization.decision_sender,
            audience="single_authorized_recipient",
            decision_pipeline_version="production-authority-v1",
            decision_type="RESPOND",
            recommended_action="conversation.reply",
            proposed_response=effective_response_snapshot,
            escalation_required=False,
            confidence=1.0,
            missing_information=[],
            requested_capabilities=[{"name": "conversation.reply"}],
            semantic_source="approved_execution_intent",
            response_source="frozen_authority",
            execution_allowed=False,
            external_delivery_allowed=False,
            reasoning_summary="Exact human-approved frozen production authority projection",
            status="APPROVED_CONTROL_ONLY",
            created_at=timestamp,
        )
        session.add(decision)
        session.flush()
        audit(
            session,
            interaction.id,
            "production_execution_lineage_materialized",
            {
                "execution_intent_id": parent.id,
                "authorization_id": authorization.id,
                "source_inbound_event_id": normalized.id,
            },
            correlation_id=authorization.correlation_id,
            causation_id=authorization.id,
            tenant_id=interaction.tenant_id,
        )
    elif (
        decision.event_id != event.id
        or decision.interaction_id != interaction.id
        or decision.proposed_response != effective_response_snapshot
    ):
        raise ProductionAuthorityDenied("PRODUCTION_LINEAGE_SEMANTIC_DRIFT")

    return materialize_production_agent_intent(
        session,
        execution_intent_id=parent.id,
        expected_fingerprint=expected_fingerprint,
        agent_decision_id=decision.id,
        effective_response_snapshot=effective_response_snapshot,
        now=timestamp,
    )


__all__ = [
    "materialize_production_agent_intent",
    "materialize_approved_production_agent_intent",
    "freeze_production_execution_intent",
    "validate_frozen_execution_intent_authority",
    "validate_materialized_execution_intent_authority",
]


def create_production_scenario_run(
    session: Session, *, agent_execution_intent_id: str, tenant_id: str,
    scenario_version_id: str, run_id: str, root_correlation_id: str,
    expires_at: datetime, now: datetime | None = None,
) -> ScenarioRunRow:
    """Create/replay the single inert production run projection."""
    agent = session.execute(select(AgentExecutionIntentRow).where(
        AgentExecutionIntentRow.id == agent_execution_intent_id).with_for_update()).scalar_one_or_none()
    if agent is None or agent.execution_intent_id is None:
        raise ProductionAuthorityDenied("PRODUCTION_AGENT_PARENT_REQUIRED")
    parent = session.get(ExecutionIntentRow, agent.execution_intent_id)
    if parent is None:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FOUND")
    semantic_scenario_id = parent.scope.get("scenario", {}).get("id")
    if scenario_version_id != semantic_scenario_id:
        raise ProductionAuthorityDenied("PRODUCTION_RUN_SCENARIO_MISMATCH")
    if parent.expires_at is None or _utc(expires_at) > _utc(parent.expires_at):
        raise ProductionAuthorityDenied("PRODUCTION_RUN_EXPIRY_WIDENING")
    validate_not_expired(expires_at, now)
    decision = session.get(AgentDecisionRow, agent.agent_decision_id)
    interaction = session.get(InteractionRow, decision.interaction_id) if decision else None
    if interaction is None or interaction.tenant_id != tenant_id:
        raise ProductionAuthorityDenied("PRODUCTION_RUN_TENANT_MISMATCH")
    existing = session.scalar(select(ScenarioRunRow).where(
        ScenarioRunRow.agent_execution_intent_id == agent.id).with_for_update())
    if existing is not None:
        if existing.tenant_id != tenant_id or existing.id != run_id:
            raise ProductionAuthorityDenied("PRODUCTION_RUN_IDEMPOTENCY_MISMATCH")
        return existing
    from attention_router.platform.scenarios import create_scenario_run
    return create_scenario_run(
        session, run_id=run_id, tenant_id=tenant_id, scenario_version_id=scenario_version_id,
        synthetic_actor_binding_id=None, agent_execution_intent_id=agent.id,
        root_correlation_id=root_correlation_id, source_sha="production-authority",
        runtime_sha="production-authority", schema_revision="0026",
        driver_revision=None, readiness_result_id=None, effect_budget_id=None,
        expires_at=expires_at, require_synthetic_actor=False, now=now)


__all__.append("create_production_scenario_run")
