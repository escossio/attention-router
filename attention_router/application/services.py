import socket
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.adapters.mocks import DeterministicLanguageAdapter, MockActionAdapter
from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.adapters.wwebjs_outbound import (
    WwebjsOutboundAdapter,
    WwebjsOutboundConfigError,
    WwebjsOutboundError,
    WwebjsOutboundPermanentError,
)
from attention_router.adapters.local_transport_outbound import local_transport_outbound
from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseStatus,
    canonical_command_text,
    parse_owner_grace_control,
    render_owner_control_confirmation,
    render_owner_control_error,
)
from attention_router.application.owner_control import (
    OwnerControlError,
    build_owner_control_signal,
    build_owner_operator_authority,
    dispatch_owner_control_signal,
)
from attention_router.application.owner_operational_control import (
    OperationalControlConflict,
    OperationalControlError,
    OperationalControlUnauthorized,
)
from attention_router.application.memory import archive_incremental_message, enqueue_memory_ingestion
from attention_router.application.execution import (
    automatic_intent_denial_reason,
    execution_gate,
    probe_transport_status,
    requires_platform_execution_safety,
    validate_platform_outbox_safety,
)
from attention_router.application.repetition import check_text_repetition
from attention_router.application.owner_reply_grace import (
    OWNER_OBSERVATION_TYPES,
    defer_decision_for_grace,
    grace_allows_interaction,
    receive_owner_outbound_observation,
)
from attention_router.application.platform.events import record_timeline_event
from attention_router.config import settings
from attention_router.core.events import OwnerCommandUnauthorized
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.attention_logic import (
    ATTENTION_LOGIC_POLICY_VERSION,
    ActorProfile,
    AttentionThresholds,
    InteractionSummary,
    classify_inbound,
    decide_attention,
)
from attention_router.domain.enums import ActionState, InteractionState
from attention_router.domain.models import ContactIdentity, new_id, now_utc
from attention_router.domain.policies import resolve_policy
from attention_router.infrastructure.models import (
    AcknowledgementRow,
    ActionAttemptRow,
    AuditEventRow,
    DecisionRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    QueueRow,
    TimerRow,
    AgentExecutionIntentRow,
    AgentDecisionRow,
    EffectConsumptionRow,
    ExecutionLeaseRow,
    ActorBindingRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.repository import (
    add_ack_timer,
    add_action,
    audit,
    create_inbound_event,
    create_interaction_row,
    find_active_actor_binding_by_external_actor_id,
    find_active_actor_binding_by_key,
    mask_identifier,
    get_active_policy_version,
    get_policy,
    interaction_to_dict,
    list_policies,
    resolve_actor_binding,
    set_state,
    to_domain_interaction,
)
from attention_router.observability.tracing import (
    inject_trace_context,
    message_trace,
    safe_set_attribute,
    set_outcome,
    start_span,
)
from attention_router.platform.execution_safety import SafetyDenied, finalize_consumed_in_transaction
from attention_router.platform.findings import (
    FindingCandidate,
    FindingCategory,
    FindingSeverity,
    record_finding,
)
from attention_router.platform.lineage import (
    EventLineage,
    LineageClassification,
    LineageDenied,
    require_scenario_actor_scope,
    require_structurally_synthetic_binding,
    require_unambiguous_stimulus_id,
)


language = DeterministicLanguageAdapter()
actions = MockActionAdapter()
wwebjs_outbound = WwebjsOutboundAdapter()


OWNER_CONTROL_OUTBOX_ACTION = "owner_control_text"


class DuplicatePayloadConflictError(ValueError):
    pass


def _transition(
    session: Session,
    row: InteractionRow,
    state: InteractionState,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    previous, next_state = set_state(row, state)
    if previous != next_state:
        audit(
            session,
            row.id,
            "state_transition",
            payload or {"event": event_type},
            row.correlation_id,
            row.causation_id,
            previous,
            next_state,
            row.policy_version_id,
        )


def create_interaction(
    session: Session,
    event_type: str,
    contact_id: str,
    contact_name: str,
    relationship_category: str,
    active_context: str | None,
    inbound_text: str,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict:
    correlation_id = correlation_id or new_id()
    contact = ContactIdentity(contact_id, contact_name, relationship_category)
    row = create_interaction_row(
        session, event_type, contact, active_context, inbound_text, correlation_id, causation_id,
        tenant_id,
    )
    # The modern decision worker owns policy, behavior, autonomy, and outbound
    # routing.  Running the historical inline decision as well would create a
    # legacy request_information outbox before the worker can fail closed.
    if not settings.agent_decision_pipeline_enabled:
        _complete_interaction_decision(session, row)
    session.flush()
    return interaction_to_dict(session, row)


def _create_owner_control_interaction(
    session: Session,
    *,
    receipt: InboundEventRow,
    owner_actor_key: str,
    canonical_text: str,
) -> InteractionRow:
    contact = ContactIdentity(
        f"owner_control:{stable_hash(f'{receipt.tenant_id}:{owner_actor_key}')[:32]}",
        "Owner Control Channel",
        "owner_control",
    )
    row = create_interaction_row(
        session,
        "OWNER_CONTROL_COMMAND",
        contact,
        "owner_control",
        canonical_text,
        receipt.correlation_id,
        receipt.id,
        receipt.tenant_id,
    )
    _transition(
        session,
        row,
        InteractionState.COMPLETED,
        "OWNER_CONTROL_COMMAND_COMPLETED",
        {"event": "OWNER_CONTROL_COMMAND_COMPLETED"},
    )
    receipt.interaction_id = row.id
    receipt.status = "PROCESSED"
    receipt.processed_at = now_utc()
    return row


def _enqueue_owner_control_confirmation(
    session: Session,
    *,
    receipt: InboundEventRow,
    interaction: InteractionRow,
    binding: ActorBindingRow,
    text: str,
) -> OutboxMessageRow:
    idempotency_key = f"owner-control:confirmation:{receipt.id}"
    existing = session.scalar(
        select(OutboxMessageRow).where(
            OutboxMessageRow.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return existing
    stamp = now_utc()
    row = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction.id,
        action_type=OWNER_CONTROL_OUTBOX_ACTION,
        destination="local_transport",
        payload={
            "external_actor_id": binding.external_actor_id,
            "message_type": "text",
            "text": text,
        },
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=idempotency_key,
        correlation_id=receipt.correlation_id,
        causation_id=receipt.id,
        execution_intent_id=None,
    )
    session.add(row)
    audit(
        session,
        interaction.id,
        "owner_control.confirmation_enqueued",
        {"outbox_id": row.id, "action_type": OWNER_CONTROL_OUTBOX_ACTION},
        receipt.correlation_id,
        receipt.id,
        origin="owner_control",
    )
    return row


def _handle_owner_control_command(
    session: Session,
    *,
    receipt: InboundEventRow,
    binding: ActorBindingRow | None,
    fallback_actor_key: str,
) -> dict[str, Any] | None:
    payload = receipt.payload or {}
    if (
        payload.get("event_origin") != "OWNER_COMMAND"
        or payload.get("owner_authenticated") is not True
    ):
        return None
    metadata = payload.get("metadata") or {}
    if (
        metadata.get("from_me") is not True
        or metadata.get("owner_self_chat") is not True
        or metadata.get("from_me_classification") != "OWNER_COMMAND"
        or metadata.get("final_from_me_classification") != "OWNER_COMMAND"
    ):
        raise OwnerControlError("OWNER_AUTHORITY_UNAVAILABLE")
    persisted_text = payload.get("content")
    if not isinstance(persisted_text, str):
        raise OwnerControlError("OWNER_CONTROL_COMMAND_TEXT_INVALID")
    parsed = parse_owner_grace_control(persisted_text)
    if parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND:
        return None

    owner_actor_key = binding.actor_key if binding is not None else fallback_actor_key
    interaction = _create_owner_control_interaction(
        session,
        receipt=receipt,
        owner_actor_key=owner_actor_key,
        canonical_text=canonical_command_text(parsed),
    )
    source_event_hash = stable_hash(receipt.external_event_id)
    audit(
        session,
        interaction.id,
        "owner_control.signal_received",
        {"source_channel": "wwebjs-owner-control", "source_event_hash": source_event_hash},
        receipt.correlation_id,
        receipt.id,
        origin="owner_control",
    )
    try:
        authority, evidence = build_owner_operator_authority(
            session,
            receipt=receipt,
            binding=binding,
        )
    except OwnerControlError as exc:
        audit(
            session,
            interaction.id,
            "owner_control.command_rejected",
            {"reason_code": str(exc)},
            receipt.correlation_id,
            receipt.id,
            origin="owner_control",
        )
        session.flush()
        result = interaction_to_dict(session, interaction)
        result["inbound_event_id"] = receipt.id
        return result

    if parsed.status == OwnerControlParseStatus.REJECTED:
        reason_code = parsed.reason_code or "CONTROL_COMMAND_INVALID_VALUE"
        audit(
            session,
            interaction.id,
            "owner_control.command_rejected",
            {"reason_code": reason_code},
            receipt.correlation_id,
            receipt.id,
            origin="owner_control",
        )
        _enqueue_owner_control_confirmation(
            session,
            receipt=receipt,
            interaction=interaction,
            binding=binding,
            text=render_owner_control_error(reason_code),
        )
    else:
        if parsed.action is None or parsed.parameters is None or binding is None:
            raise OwnerControlError("OWNER_CONTROL_NORMALIZATION_INVALID")
        signal = build_owner_control_signal(
            receipt=receipt,
            binding=binding,
            action=parsed.action,
            parameters=parsed.parameters,
            authority_evidence=evidence,
        )
        audit(
            session,
            interaction.id,
            "owner_control.command_normalized",
            {"signal_kind": signal.signal_kind.value, "action": signal.action.value},
            receipt.correlation_id,
            receipt.id,
            origin="owner_control",
        )
        try:
            dispatched = dispatch_owner_control_signal(
                session,
                signal=signal,
                authority=authority,
            )
        except (
            OperationalControlConflict,
            OperationalControlError,
            OperationalControlUnauthorized,
            OwnerCommandUnauthorized,
            OwnerControlError,
        ) as exc:
            reason_code = str(exc)
            audit(
                session,
                interaction.id,
                "owner_control.command_rejected",
                {"action": signal.action.value, "reason_code": reason_code},
                receipt.correlation_id,
                receipt.id,
                origin="owner_control",
            )
            confirmation = render_owner_control_error(reason_code)
        else:
            audit(
                session,
                interaction.id,
                "owner_control.command_dispatched",
                {
                    "action": signal.action.value,
                    "policy_id": dispatched.policy_id,
                    "changed": dispatched.mutation.changed,
                    "duplicate": dispatched.mutation.duplicate,
                },
                receipt.correlation_id,
                receipt.id,
                origin="owner_control",
            )
            confirmation = render_owner_control_confirmation(
                dispatched,
                action=signal.action,
            )
        _enqueue_owner_control_confirmation(
            session,
            receipt=receipt,
            interaction=interaction,
            binding=binding,
            text=confirmation,
        )
    session.flush()
    result = interaction_to_dict(session, interaction)
    result["inbound_event_id"] = receipt.id
    return result


@message_trace
def receive_inbound_event(
    session: Session,
    source: str,
    external_event_id: str,
    event_type: str,
    contact_id: str,
    contact_name: str,
    relationship_category: str,
    active_context: str | None,
    inbound_text: str,
    payload: dict[str, Any] | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    received_at: datetime | None = None,
    actor_binding: ActorBindingRow | None = None,
) -> dict:
    correlation_id = new_id()
    normalized = payload or {
        "event_type": event_type,
        "contact_id": contact_id,
        "contact_name": contact_name,
        "relationship_category": relationship_category,
        "active_context": active_context,
        "inbound_text": inbound_text,
    }
    payload_hash = stable_hash(normalized)
    try:
        receipt = create_inbound_event(
            session, source, external_event_id, event_type, normalized, correlation_id, tenant_id,
            received_at,
        )
    except IntegrityError:
        session.rollback()
        existing = session.scalars(
            select(InboundEventRow).where(
                InboundEventRow.tenant_id == tenant_id,
                InboundEventRow.source == source,
                InboundEventRow.external_event_id == external_event_id,
            )
        ).one()
        if existing.payload_hash != payload_hash:
            audit(
                session,
                existing.interaction_id,
                "duplicate_payload_conflict",
                {"source": source, "external_event_id": external_event_id, "receipt_id": existing.id},
                existing.correlation_id,
                existing.id,
            )
            session.commit()
            raise DuplicatePayloadConflictError("same source/external_event_id received with different payload")
        audit(
            session,
            existing.interaction_id,
            "duplicate_replay",
            {"source": source, "external_event_id": external_event_id, "receipt_id": existing.id},
            existing.correlation_id,
            existing.id,
        )
        if existing.interaction_id:
            return interaction_to_dict(session, session.get(InteractionRow, existing.interaction_id))
        return {"receipt_id": existing.id, "status": existing.status, "interaction_id": None}

    owner_control_result = _handle_owner_control_command(
        session,
        receipt=receipt,
        binding=actor_binding,
        fallback_actor_key=contact_id,
    )
    if owner_control_result is not None:
        return owner_control_result

    metadata = (payload or {}).get("metadata") or {}
    lineage_classification = str(
        (payload or {}).get("lineage_classification") or "HISTORICAL_UNKNOWN"
    )
    lineage_metadata = {
        "lineage_classification": lineage_classification,
        "scenario_id": (payload or {}).get("scenario_id"),
        "scenario_run_id": (payload or {}).get("scenario_run_id"),
        "scenario_step_run_id": (payload or {}).get("scenario_step_run_id"),
        "stimulus_id": (payload or {}).get("stimulus_id"),
    }
    # An authenticated self-chat Owner Command is an inbound control event,
    # even though WhatsApp marks the transport message as from_me.
    if bool(metadata.get("from_me")) and not bool((payload or {}).get("owner_authenticated")):
        try:
            if settings.persistent_memory_enabled and (
                not settings.persistent_memory_canary_binding_id or
                settings.persistent_memory_canary_binding_id == contact_id
            ):
                with session.begin_nested():
                    sent_at = metadata.get("occurred_at") or metadata.get("sent_at") or now_utc()
                    if isinstance(sent_at, str):
                        from datetime import datetime
                        sent_at = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                    with start_span("memory.archive") as memory_span:
                        archived, created = archive_incremental_message(
                            session, source=source, source_account=str(metadata.get("source_account", "default")),
                            thread_key=str(metadata.get("thread_key", contact_id)), thread_type=str(metadata.get("thread_type", "DIRECT")),
                            source_message_id=external_event_id, actor_key=contact_id, display_name=contact_name,
                            text=inbound_text, sent_at=sent_at, from_me=True, metadata=metadata,
                            **lineage_metadata,
                        )
                        safe_set_attribute(memory_span, "attention.memory.created", created)
                        set_outcome(memory_span, "ARCHIVED" if created else "DUPLICATE")
                audit(session, None, "memory.message_archived", {"created": created, "from_me": True},
                      receipt.correlation_id, receipt.id, origin="memory")
            receipt.status = "PROCESSED"
            receipt.processed_at = now_utc()
            audit(session, None, "inbound_from_me_ignored", {"receipt_id": receipt.id}, receipt.correlation_id, receipt.id)
            session.flush()
            return {"receipt_id": receipt.id, "status": receipt.status, "interaction_id": None,
                    "correlation_id": receipt.correlation_id}
        except Exception as exc:
            audit(session, None, "memory.archive_failed", {"error_type": type(exc).__name__},
                  receipt.correlation_id, receipt.id, origin="memory")
        receipt.status = "PROCESSED"
        receipt.processed_at = now_utc()
        session.flush()
        return {"receipt_id": receipt.id, "status": receipt.status, "interaction_id": None,
                "correlation_id": receipt.correlation_id}

    try:
        with start_span("inbound.receive") as inbound_span:
            result = create_interaction(
                session, event_type, contact_id, contact_name, relationship_category,
                active_context, inbound_text, receipt.correlation_id, receipt.id,
                tenant_id,
            )
            safe_set_attribute(inbound_span, "attention.message_length", len(inbound_text))
            safe_set_attribute(inbound_span, "attention.message_text_sha256", stable_hash(inbound_text))
            safe_set_attribute(inbound_span, "attention.inbound_event_id", receipt.id)
            safe_set_attribute(inbound_span, "attention.interaction_id", result["id"])
            set_outcome(inbound_span, "ACCEPTED")
        receipt.interaction_id = result["id"]
        receipt.status = "PROCESSED"
        receipt.processed_at = now_utc()
        if settings.agent_decision_pipeline_enabled:
            interaction = session.get(InteractionRow, result["id"])
            if not defer_decision_for_grace(session, receipt, interaction):
                carrier: dict[str, str] = {}
                inject_trace_context(carrier)
                session.add(
                    QueueRow(
                        id=f"decision:{receipt.id}",
                        kind="decision",
                        payload={"event_id": receipt.id, "interaction_id": result["id"], "observability": {"trace_context": carrier}},
                        status="PENDING",
                        created_at=now_utc(),
                    )
                )
        if settings.persistent_memory_enabled and (
            not settings.persistent_memory_canary_binding_id or
            settings.persistent_memory_canary_binding_id == contact_id
        ):
            try:
                with session.begin_nested():
                    metadata = (payload or {}).get("metadata") or {}
                    sent_at = metadata.get("occurred_at") or metadata.get("sent_at")
                    if isinstance(sent_at, str):
                        from datetime import datetime
                        sent_at = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                    sent_at = sent_at or now_utc()
                    with start_span("memory.archive") as memory_span:
                        archived, created = archive_incremental_message(
                            session, source=source, source_account=str(metadata.get("source_account", "default")),
                            thread_key=str(metadata.get("thread_key", contact_id)), thread_type=str(metadata.get("thread_type", "DIRECT")),
                            source_message_id=external_event_id, actor_key=contact_id, display_name=contact_name,
                            text=inbound_text, sent_at=sent_at, from_me=bool(metadata.get("from_me", False)), metadata=metadata,
                            tenant_id=tenant_id,
                            **lineage_metadata,
                        )
                        safe_set_attribute(memory_span, "attention.memory.created", created)
                        set_outcome(memory_span, "ARCHIVED" if created else "DUPLICATE")
                    if settings.memory_ingestion_enabled and created:
                        enqueue_memory_ingestion(session, archived.id)
                audit(session, result["id"], "memory.message_archived", {"created": created, "from_me": archived.from_me},
                      receipt.correlation_id, receipt.id, origin="memory")
            except Exception as exc:
                audit(session, result["id"], "memory.archive_failed", {"error_type": type(exc).__name__},
                      receipt.correlation_id, receipt.id, origin="memory")
        audit(session, result["id"], "inbound_event_processed", {"receipt_id": receipt.id}, receipt.correlation_id, receipt.id)
        session.flush()
        result["inbound_event_id"] = receipt.id
        return result
    except Exception as exc:
        receipt.status = "FAILED"
        receipt.error = str(exc)
        audit(
            session,
            None,
            "inbound_event_failed",
            {"receipt_id": receipt.id, "error": str(exc)},
            receipt.correlation_id,
            receipt.id,
            tenant_id=tenant_id,
        )
        raise


@message_trace
def receive_normalized_inbound_event(session: Session, event: NormalizedInboundEvent) -> dict:
    if event.event_origin in OWNER_OBSERVATION_TYPES:
        result = receive_owner_outbound_observation(session, event)
        if (
            event.event_origin == "ROUTER_AUTOMATED_OUTBOUND_OBSERVED"
            and event.metadata.get("authenticated_owner_self_chat_target") is True
            and session.scalar(
                select(AuditEventRow.id).where(
                    AuditEventRow.tenant_id == event.tenant_id,
                    AuditEventRow.event_type == "owner_control.echo_suppressed",
                    AuditEventRow.causation_id == result["receipt_id"],
                )
            )
            is None
        ):
            audit(
                session,
                None,
                "owner_control.echo_suppressed",
                {"classification": event.event_origin},
                result["correlation_id"],
                result["receipt_id"],
                origin="owner_control",
                tenant_id=event.tenant_id,
            )
        return result
    binding = resolve_actor_binding(session, event.source, event.actor_id, event.tenant_id)
    if binding is not None:
        event = _apply_structural_synthetic_binding_lineage(session, event, binding)
    actor_id = event.actor_id
    actor_name = event.actor_display_name
    actor_category = event.actor_category
    active_context = event.active_context
    if binding:
        actor_id = binding.actor_key
        actor_name = binding.display_name or binding.actor_key
        actor_category = binding.actor_category
        active_context = binding.active_context or event.active_context
        audit(
            session,
            None,
            "actor_resolved",
            {
                "source": event.source,
                "external_actor_id": mask_identifier(event.actor_id),
                "actor_key": binding.actor_key,
            },
            event.correlation_id,
            None,
            origin="identity",
            tenant_id=event.tenant_id,
        )
    else:
        actor_name = "Unknown actor"
        actor_category = "unknown"
        audit(
            session,
            None,
            "actor_unresolved",
            {"source": event.source, "external_actor_id": mask_identifier(event.actor_id)},
            event.correlation_id,
            None,
            origin="identity",
            tenant_id=event.tenant_id,
        )
    audit(
        session,
        None,
        "adapter_normalized",
        {
            "source": event.source,
            "external_event_id": event.external_event_id,
            "schema_version": event.schema_version,
            "event_origin": event.event_origin or "EXTERNAL_INBOUND",
            "owner_authenticated": event.owner_authenticated,
        },
        event.correlation_id,
        None,
        origin="ingress",
        tenant_id=event.tenant_id,
    )
    return receive_inbound_event(
        session,
        source=event.source,
        external_event_id=event.external_event_id,
        event_type=event.event_type,
        contact_id=actor_id,
        contact_name=actor_name,
        relationship_category=actor_category,
        active_context=active_context,
        inbound_text=event.content,
        payload=event.normalized_payload,
        tenant_id=event.tenant_id,
        received_at=event.received_at,
        actor_binding=binding,
    )


def _apply_structural_synthetic_binding_lineage(
    session: Session,
    event: NormalizedInboundEvent,
    binding: ActorBindingRow,
) -> NormalizedInboundEvent:
    binding_metadata = binding.binding_metadata or {}
    declares_synthetic = (
        binding.actor_category.upper() == "SYNTHETIC_TEST_ACTOR"
        or binding_metadata.get("synthetic") is not None
        or binding_metadata.get("lineage_classification")
        == LineageClassification.SYNTHETIC.value
    )
    if not declares_synthetic:
        return event
    require_structurally_synthetic_binding(binding)
    active_runs = session.scalars(
        select(ScenarioRunRow).where(
            ScenarioRunRow.tenant_id == event.tenant_id,
            ScenarioRunRow.synthetic_actor_binding_id == binding.id,
            ScenarioRunRow.status.in_(["ARMED", "RUNNING"]),
            ScenarioRunRow.expires_at > now_utc(),
        )
    ).all()
    if len(active_runs) != 1:
        reason = (
            "SYNTHETIC_ACTOR_NO_ACTIVE_SCENARIO"
            if not active_runs
            else "SYNTHETIC_ACTOR_SCENARIO_SCOPE_COLLISION"
        )
        raise LineageDenied(reason)
    run = active_runs[0]
    version = session.get(ScenarioVersionRow, run.scenario_version_id)
    definition = (
        session.get(ScenarioDefinitionRow, version.scenario_definition_id)
        if version
        else None
    )
    if version is None or definition is None or version.tenant_id != event.tenant_id:
        raise LineageDenied("SYNTHETIC_SCENARIO_PROVENANCE_MISSING")
    stimulus_steps = session.scalars(
        select(ScenarioStepRunRow).where(
            ScenarioStepRunRow.tenant_id == event.tenant_id,
            ScenarioStepRunRow.scenario_run_id == run.id,
            ScenarioStepRunRow.step_kind == "STIMULUS",
            ScenarioStepRunRow.status.in_(
                ["RUNNING", "VERIFYING", "RECONCILIATION_REQUIRED", "PASSED"]
            ),
        )
    ).all()
    stimulus_id = require_unambiguous_stimulus_id(
        event_stimulus_id=event.stimulus_id,
        binding_stimulus_id=binding_metadata.get("stimulus_id"),
        step_stimulus_ids=tuple(step.idempotency_key for step in stimulus_steps),
    )
    owner_actor_id = session.scalar(
        select(ActorBindingRow.actor_key).where(
            ActorBindingRow.tenant_id == event.tenant_id,
            ActorBindingRow.is_active.is_(True),
            ActorBindingRow.actor_category == "owner",
        )
    )
    lineage = EventLineage(
        tenant_id=event.tenant_id,
        actor_id=binding.actor_key,
        classification=LineageClassification.SYNTHETIC,
        scenario_id=definition.scenario_key,
        scenario_run_id=run.id,
        stimulus_id=stimulus_id,
    )
    require_scenario_actor_scope(
        lineage,
        scenario_tenant_id=run.tenant_id,
        owner_actor_id=owner_actor_id,
        actor_category=binding.actor_category,
    )
    if event.lineage_classification == LineageClassification.SYNTHETIC.value:
        if (
            event.scenario_run_id != run.id
            or event.scenario_id != definition.scenario_key
            or event.stimulus_id != stimulus_id
        ):
            raise LineageDenied("SYNTHETIC_EVENT_SCENARIO_LINEAGE_MISMATCH")
        return event
    return event.model_copy(
        update={
            "lineage_classification": LineageClassification.SYNTHETIC.value,
            "scenario_id": definition.scenario_key,
            "scenario_run_id": run.id,
            "stimulus_id": stimulus_id,
        }
    )


def _complete_interaction_decision(session: Session, row: InteractionRow) -> None:
    _transition(session, row, InteractionState.DECIDING, "deciding")
    binding = find_active_actor_binding_by_key(session, row.contact_id, row.tenant_id)
    audience = (binding.binding_metadata or {}).get("audience") if binding else None
    resolution = resolve_policy(
        list_policies(session, row.tenant_id),
        row.contact_id,
        row.relationship_category,
        row.active_context,
        audience=audience,
        binding_id=binding.id if binding else None,
    )
    policy = resolution.winner
    policy_version = get_active_policy_version(session, policy.identifier, row.tenant_id)
    row.policy_id = policy.identifier
    row.policy_version_id = policy_version.id
    row.lia_speech = language.render_reply(to_domain_interaction(row), policy)
    session.add(
        DecisionRow(
            id=new_id(),
            interaction_id=row.id,
            policy_id=policy.identifier,
            policy_version_id=policy_version.id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            matched_rules=resolution.matched,
            reason=resolution.reason,
            created_at=now_utc(),
        )
    )
    audit(
        session,
        row.id,
        "policy_resolved",
        {"winner": policy.identifier, "matched": resolution.matched, "reason": resolution.reason},
        row.correlation_id,
        row.causation_id,
        policy_version_id=policy_version.id,
    )
    audit(
        session,
        row.id,
        "decision_made",
        {"policy_id": policy.identifier, "policy_version_id": policy_version.id},
        row.correlation_id,
        row.causation_id,
        policy_version_id=policy_version.id,
    )
    record_attention_logic_decision(session, row)
    dispatch_next_action(session, row)


def _attention_thresholds() -> AttentionThresholds:
    return AttentionThresholds(
        recent_window_seconds=settings.attention_recent_window_seconds,
        rapid_repeat_seconds=settings.attention_rapid_repeat_seconds,
        persistent_message_count=settings.attention_persistent_message_count,
        short_text_max_chars=settings.attention_short_text_max_chars,
        medium_text_max_chars=settings.attention_medium_text_max_chars,
    )


def _actor_profile(session: Session, row: InteractionRow) -> ActorProfile:
    binding = find_active_actor_binding_by_key(
        session, row.contact_id, row.tenant_id
    ) or find_active_actor_binding_by_external_actor_id(
        session, row.contact_id, row.tenant_id
    )
    metadata = binding.binding_metadata if binding else {}
    return ActorProfile(
        actor_alias=(metadata.get("actor_alias") or row.contact_id),
        display_name=(metadata.get("display_name") or row.contact_name),
        relationship=(metadata.get("relationship") or row.relationship_category),
        role=metadata.get("role"),
        priority=(metadata.get("priority") or "normal"),
        test_allowed=bool(metadata.get("test_allowed", False)),
        policy_id=(metadata.get("policy_id") or "attention_default_v1"),
    )


def _recent_interaction_summaries(session: Session, row: InteractionRow) -> list[InteractionSummary]:
    cutoff = row.created_at - timedelta(seconds=settings.attention_recent_window_seconds)
    binding = find_active_actor_binding_by_key(
        session, row.contact_id, row.tenant_id
    ) or find_active_actor_binding_by_external_actor_id(
        session, row.contact_id, row.tenant_id
    )
    actor_ids = {row.contact_id}
    if binding:
        actor_ids.add(binding.actor_key)
        actor_ids.add(binding.external_actor_id)
    rows = session.scalars(
        select(InteractionRow)
        .where(
            InteractionRow.tenant_id == row.tenant_id,
            InteractionRow.contact_id.in_(actor_ids),
            InteractionRow.event_type == "message",
            InteractionRow.created_at >= cutoff,
            InteractionRow.created_at <= row.created_at,
        )
        .order_by(InteractionRow.created_at)
    ).all()
    return [InteractionSummary(interaction_id=item.id, created_at=item.created_at) for item in rows]


def build_attention_logic_decision(session: Session, row: InteractionRow) -> dict[str, Any]:
    profile = _actor_profile(session, row)
    thresholds = _attention_thresholds()
    signals = classify_inbound(
        row.inbound_text,
        profile.priority,
        InteractionSummary(interaction_id=row.id, created_at=row.created_at),
        _recent_interaction_summaries(session, row),
        thresholds,
    )
    decision = decide_attention(signals)
    return {
        "actor_profile": {
            "actor_alias": profile.actor_alias,
            "display_name": profile.display_name,
            "relationship": profile.relationship,
            "role": profile.role,
            "priority": profile.priority,
            "test_allowed": profile.test_allowed,
            "policy_id": profile.policy_id,
        },
        "policy_version": ATTENTION_LOGIC_POLICY_VERSION,
        "thresholds": {
            "recent_window_seconds": thresholds.recent_window_seconds,
            "rapid_repeat_seconds": thresholds.rapid_repeat_seconds,
            "persistent_message_count": thresholds.persistent_message_count,
            "short_text_max_chars": thresholds.short_text_max_chars,
            "medium_text_max_chars": thresholds.medium_text_max_chars,
        },
        "signals": signals,
        "decision": decision,
    }


def record_attention_logic_decision(session: Session, row: InteractionRow) -> dict[str, Any]:
    result = build_attention_logic_decision(session, row)
    audit(
        session,
        row.id,
        "attention_logic_decision_v1",
        {
            "actor_alias": result["actor_profile"]["actor_alias"],
            "display_name": result["actor_profile"]["display_name"],
            "policy_version": result["policy_version"],
            "signals": result["signals"],
            "attention_level": result["decision"]["attention_level"],
            "reason_codes": result["decision"]["reason_codes"],
            "suggested_action": result["decision"]["suggested_action"],
            "suggested_reply_profile": result["decision"]["suggested_reply_profile"],
            "escalation_candidate": result["decision"]["escalation_candidate"],
            "auto_action_enabled": False,
        },
        row.correlation_id,
        row.causation_id,
        policy_version_id=row.policy_version_id,
        origin="attention_logic",
    )
    return result


def dispatch_next_action(session: Session, row: InteractionRow) -> ActionAttemptRow | None:
    policy = get_policy(session, row.policy_id, row.tenant_id)
    existing = session.scalars(
        select(ActionAttemptRow)
        .where(ActionAttemptRow.interaction_id == row.id)
        .order_by(ActionAttemptRow.step_index)
    ).all()
    attempted_steps = {attempt.step_index for attempt in existing}
    for index, action_key in enumerate(policy.escalation_steps):
        if index not in attempted_steps:
            repetition = check_text_repetition(session, row)
            if repetition.suppress:
                _transition(session, row, InteractionState.COMPLETED, "response_suppressed", {"reason": repetition.reason})
                audit(session, row.id, "response_suppressed", {
                    "reason": repetition.reason,
                    "response_objective": repetition.response_objective,
                    "objective_already_satisfied": repetition.objective_already_satisfied,
                    "previous_useful_response_generated": repetition.previous_useful_response_generated,
                    "previous_useful_response_delivered": repetition.previous_useful_response_delivered,
                    "semantic_repeat": repetition.suppress,
                    "state_changed": False,
                    "semantic_response_signature": repetition.signature,
                }, row.correlation_id, row.causation_id, origin="conversation_repetition_guard")
                return None
            _transition(session, row, InteractionState.ACTING, "acting", {"action_key": action_key})
            action = add_action(session, row.id, action_key, index, row.correlation_id, row.causation_id)
            _transition(session, row, InteractionState.WAITING_ACK, "waiting_ack", {"action_id": action.id})
            add_ack_timer(session, row.id, action.id, policy.ack_timeout_seconds, row.correlation_id, action.id)
            return action
    _transition(session, row, InteractionState.FAILED, "failed", {"reason": "no_authorized_action_left"})
    audit(session, row.id, "escalation_failed", {"reason": "no_authorized_action_left"}, row.correlation_id, row.causation_id)
    return None


def human_reply(session: Session, interaction_id: str, text: str) -> dict:
    row = session.get(InteractionRow, interaction_id)
    _transition(session, row, InteractionState.CANCELED_BY_HUMAN_REPLY, "human_reply")
    session.execute(
        update(TimerRow).where(TimerRow.interaction_id == interaction_id, TimerRow.status.in_(["pending", "PENDING", "RETRY", "PROCESSING"])).values(status="CANCELED")
    )
    session.execute(
        update(QueueRow)
        .where(QueueRow.payload["interaction_id"].as_string() == interaction_id, QueueRow.status.in_(["pending", "PENDING", "WAITING_TRANSCRIPTION"]))
        .values(status="CANCELED")
    )
    audit(session, interaction_id, "human_reply_canceled", {"text": text}, row.correlation_id, row.causation_id)
    session.flush()
    return interaction_to_dict(session, row)


def enqueue_wwebjs_controlled_outbound(session: Session, interaction_id: str, text: str) -> OutboxMessageRow:
    receipt = session.scalars(
        select(InboundEventRow)
        .where(InboundEventRow.interaction_id == interaction_id, InboundEventRow.source == settings.internal_ingress_source)
        .order_by(InboundEventRow.received_at.desc())
        .limit(1)
    ).first()
    if not receipt:
        raise ValueError("interaction is not backed by a wwebjs inbound event")
    external_actor_id = receipt.payload.get("external_actor_id") or receipt.payload.get("actor_id")
    if not external_actor_id:
        raise ValueError("wwebjs external_actor_id unavailable for outbound")
    binding = resolve_actor_binding(
        session, settings.internal_ingress_source, external_actor_id, receipt.tenant_id
    )
    if binding and binding.binding_metadata.get("test_allowed") is False:
        audit(
            session,
            interaction_id,
            "wwebjs_controlled_outbound_rejected",
            {
                "reason": "actor_test_not_allowed",
                "actor_alias": binding.binding_metadata.get("actor_alias") or binding.actor_key,
                "display_name": binding.display_name,
            },
            receipt.correlation_id,
            receipt.id,
            origin="manual_operator",
        )
        raise ValueError("actor is not allowed as controlled test target")
    row = session.get(InteractionRow, interaction_id)
    if not row:
        raise ValueError("interaction not found")
    stamp = now_utc()
    outbox = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction_id,
        action_type="wwebjs_outbound_text",
        destination="wwebjs",
        payload={"external_actor_id": external_actor_id, "message_type": "text", "text": text},
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=f"wwebjs:{interaction_id}:{stable_hash(text)}",
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
    )
    session.add(outbox)
    audit(
        session,
        interaction_id,
        "wwebjs_controlled_outbound_created",
        {
            "outbox_id": outbox.id,
            "destination": "wwebjs",
            "idempotency_key": outbox.idempotency_key,
            "external_actor_id": mask_identifier(external_actor_id),
        },
        row.correlation_id,
        row.causation_id,
    )
    session.flush()
    return outbox


def _wwebjs_receipt_for_manual_reply(session: Session, interaction_id: str) -> InboundEventRow:
    row = session.get(InteractionRow, interaction_id)
    if not row:
        raise ValueError("interaction not found")
    receipt = session.scalars(
        select(InboundEventRow)
        .where(InboundEventRow.interaction_id == interaction_id, InboundEventRow.source == settings.internal_ingress_source)
        .order_by(InboundEventRow.received_at.desc())
        .limit(1)
    ).first()
    if not receipt:
        raise ValueError("interaction is not backed by a wwebjs inbound event")
    if receipt.event_type != "message":
        raise ValueError("interaction is not a supported individual message")
    return receipt


def validate_wwebjs_manual_reply_target(session: Session, interaction_id: str) -> tuple[InteractionRow, InboundEventRow, str]:
    row = session.get(InteractionRow, interaction_id)
    if not row:
        raise ValueError("interaction not found")
    receipt = _wwebjs_receipt_for_manual_reply(session, interaction_id)
    payload = receipt.payload or {}
    metadata = payload.get("metadata") or {}
    external_actor_id = payload.get("external_actor_id") or payload.get("actor_id")
    if not external_actor_id:
        raise ValueError("wwebjs external_actor_id unavailable for outbound")
    lowered_actor = external_actor_id.lower()
    if lowered_actor.endswith("@g.us") or metadata.get("is_group") is True or payload.get("is_group") is True:
        raise ValueError("group interactions are not supported for manual reply")
    if lowered_actor == "status@broadcast" or metadata.get("is_status") is True or payload.get("is_status") is True:
        raise ValueError("status interactions are not supported for manual reply")
    if payload.get("channel") not in {None, "whatsapp"}:
        raise ValueError("interaction channel is not supported for manual reply")
    if payload.get("message_type") not in {None, "text", "chat"} and payload.get("has_media") is not True:
        raise ValueError("interaction message type is not supported for manual reply")
    return row, receipt, external_actor_id


def enqueue_wwebjs_manual_reply(
    session: Session,
    interaction_id: str,
    text: str,
    idempotency_key: str | None = None,
) -> OutboxMessageRow:
    row, _receipt, external_actor_id = validate_wwebjs_manual_reply_target(session, interaction_id)
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("manual reply text is required")
    idempotency_key = idempotency_key or f"wwebjs:manual:{interaction_id}:{stable_hash(clean_text)}"
    existing = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.idempotency_key == idempotency_key)).first()
    if existing:
        return existing
    stamp = now_utc()
    audit(
        session,
        interaction_id,
        "manual_reply_requested",
        {"destination": "wwebjs", "idempotency_key": idempotency_key, "external_actor_id": mask_identifier(external_actor_id)},
        row.correlation_id,
        row.causation_id,
        origin="manual_operator",
    )
    outbox = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction_id,
        action_type="wwebjs_manual_reply_text",
        destination="wwebjs",
        payload={"external_actor_id": external_actor_id, "message_type": "text", "text": clean_text, "origin": "manual_operator"},
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=idempotency_key,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
    )
    session.add(outbox)
    session.flush()
    audit(
        session,
        interaction_id,
        "manual_reply_enqueued",
        {"outbox_id": outbox.id, "destination": "wwebjs", "idempotency_key": outbox.idempotency_key},
        row.correlation_id,
        row.causation_id,
        origin="manual_operator",
    )
    session.flush()
    return outbox


def action_executed(session: Session, action_id: str) -> dict:
    action = session.get(ActionAttemptRow, action_id)
    if action.state != ActionState.ACKNOWLEDGED.value:
        action.state = ActionState.EXECUTED.value
        action.updated_at = now_utc()
    row = session.get(InteractionRow, action.interaction_id)
    audit(session, row.id, "action_executed_without_ack", {"action_id": action.id}, row.correlation_id, action.id)
    session.flush()
    return interaction_to_dict(session, row)


def action_failed(session: Session, action_id: str) -> dict:
    action = session.get(ActionAttemptRow, action_id)
    if action.state == ActionState.FAILED.value:
        return interaction_to_dict(session, session.get(InteractionRow, action.interaction_id))
    action.state = ActionState.FAILED.value
    action.updated_at = now_utc()
    row = session.get(InteractionRow, action.interaction_id)
    _transition(session, row, InteractionState.ESCALATING, "action_failed", {"action_id": action.id})
    session.execute(
        update(TimerRow).where(TimerRow.action_attempt_id == action_id, TimerRow.status.in_(["pending", "PENDING", "RETRY", "PROCESSING"])).values(status="CANCELED")
    )
    audit(session, row.id, "action_failed", {"action_id": action.id, "action_key": action.action_key}, row.correlation_id, action.id)
    dispatch_next_action(session, row)
    session.flush()
    return interaction_to_dict(session, row)


def acknowledge_action(session: Session, action_id: str, source: str = "simulator") -> dict:
    action = session.get(ActionAttemptRow, action_id)
    if action.state == ActionState.ACKNOWLEDGED.value:
        return interaction_to_dict(session, session.get(InteractionRow, action.interaction_id))
    action.state = ActionState.ACKNOWLEDGED.value
    action.updated_at = now_utc()
    row = session.get(InteractionRow, action.interaction_id)
    _transition(session, row, InteractionState.ACKNOWLEDGED, "acknowledged", {"action_id": action.id})
    session.add(
        AcknowledgementRow(
            id=new_id(),
            interaction_id=row.id,
            action_attempt_id=action.id,
            source=source,
            created_at=now_utc(),
        )
    )
    session.execute(
        update(TimerRow).where(TimerRow.interaction_id == row.id, TimerRow.status.in_(["pending", "PENDING", "RETRY", "PROCESSING"])).values(status="CANCELED")
    )
    session.execute(
        update(QueueRow)
        .where(QueueRow.payload["interaction_id"].as_string() == row.id, QueueRow.status.in_(["pending", "PENDING"]))
        .values(status="CANCELED")
    )
    audit(session, row.id, "acknowledged", {"action_id": action.id, "source": source}, row.correlation_id, action.id)
    session.flush()
    return interaction_to_dict(session, row)


def worker_id() -> str:
    return f"{socket.gethostname()}:{new_id()}"


def claim_due_timers(session: Session, worker: str, limit: int = 10) -> list[TimerRow]:
    cutoff = now_utc() - timedelta(seconds=settings.worker_timer_lease_seconds)
    query = (
        select(TimerRow)
        .where(
            or_(
                TimerRow.status.in_(["pending", "PENDING", "RETRY"]),
                (TimerRow.status == "PROCESSING") & (TimerRow.claimed_at < cutoff),
            ),
            TimerRow.due_at <= now_utc(),
            or_(TimerRow.next_attempt_at.is_(None), TimerRow.next_attempt_at <= now_utc()),
        )
        .order_by(TimerRow.due_at)
        .limit(limit)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    timers = session.scalars(query).all()
    for timer in timers:
        timer.status = "PROCESSING"
        timer.claimed_at = now_utc()
        timer.claimed_by = worker
        timer.attempt_count = (timer.attempt_count or 0) + 1
        audit(session, timer.interaction_id, "timer_claimed", {"timer_id": timer.id, "worker": worker}, timer.correlation_id, timer.causation_id)
    session.flush()
    return timers


def process_claimed_timer(session: Session, timer: TimerRow) -> None:
    row = session.get(InteractionRow, timer.interaction_id)
    try:
        if row.state in {
            InteractionState.ACKNOWLEDGED.value,
            InteractionState.CANCELED_BY_HUMAN_REPLY.value,
            InteractionState.COMPLETED.value,
            InteractionState.FAILED.value,
        }:
            timer.status = "DONE"
            timer.completed_at = now_utc()
            audit(session, row.id, "timer_completed", {"timer_id": timer.id, "terminal": True}, timer.correlation_id, timer.causation_id)
            return
        if timer.action_attempt_id:
            action = session.get(ActionAttemptRow, timer.action_attempt_id)
            if action.state in {ActionState.DISPATCHED.value, ActionState.EXECUTED.value, ActionState.REQUESTED.value}:
                _transition(session, row, InteractionState.ESCALATING, "ack_timeout", {"action_id": action.id})
                audit(session, row.id, "ack_timeout", {"action_id": action.id, "action_key": action.action_key}, row.correlation_id, action.id)
                dispatch_next_action(session, row)
        timer.status = "DONE"
        timer.completed_at = now_utc()
        audit(session, row.id, "timer_completed", {"timer_id": timer.id}, timer.correlation_id, timer.causation_id)
    except Exception as exc:
        timer.status = "RETRY" if timer.attempt_count < 3 else "FAILED"
        timer.last_error = str(exc)
        timer.next_attempt_at = now_utc() + timedelta(seconds=5)
        audit(session, timer.interaction_id, "timer_failed", {"timer_id": timer.id, "error": str(exc)}, timer.correlation_id, timer.causation_id)
        raise


def process_due_timers(session: Session, worker: str | None = None, limit: int = 10) -> int:
    worker = worker or worker_id()
    timers = claim_due_timers(session, worker, limit)
    count = 0
    for timer in timers:
        process_claimed_timer(session, timer)
        count += 1
    session.flush()
    return count


def claim_outbox(session: Session, worker: str, limit: int = 10) -> list[OutboxMessageRow]:
    cutoff = now_utc() - timedelta(seconds=settings.worker_timer_lease_seconds)
    stale_query = select(OutboxMessageRow).where(
        OutboxMessageRow.status == "PROCESSING",
        OutboxMessageRow.claimed_at < cutoff,
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        stale_query = stale_query.with_for_update(skip_locked=True)
    for stale in session.scalars(stale_query).all():
        if _is_external_network_outbox(stale):
            _mark_external_ambiguous(
                session,
                stale,
                "STALE_PROCESSING_OUTCOME_UNKNOWN",
            )
        else:
            stale.status = "RETRY"
            stale.last_error = "STALE_PROCESSING_RECLAIMED"

    # A network-attempted external row is never reclaimed from RETRY.  Older
    # rows may carry this legacy state, so quarantine them before selection.
    retry_query = select(OutboxMessageRow).where(OutboxMessageRow.status == "RETRY")
    if session.bind and session.bind.dialect.name == "postgresql":
        retry_query = retry_query.with_for_update(skip_locked=True)
    for retry in session.scalars(retry_query).all():
        if _is_external_network_outbox(retry):
            _mark_external_ambiguous(session, retry, "LEGACY_EXTERNAL_RETRY_QUARANTINED")

    candidate_query = (
        select(OutboxMessageRow)
        .where(OutboxMessageRow.status.in_(["PENDING", "RETRY"]), OutboxMessageRow.available_at <= now_utc())
        # Control confirmations/manual authority remain dispatchable even with
        # more than one batch of frozen policy-authorized outbounds.
        .order_by(case((OutboxMessageRow.execution_intent_id.in_(
            select(AgentExecutionIntentRow.id).where(
                AgentExecutionIntentRow.authorization_source == "POLICY_AUTONOMY"
            )
        ), 1), else_=0), OutboxMessageRow.created_at)
        .limit(limit)
    )
    candidates = session.scalars(candidate_query).all()
    rows: list[OutboxMessageRow] = []
    for candidate in candidates:
        intent = session.get(AgentExecutionIntentRow, candidate.execution_intent_id) if candidate.execution_intent_id else None
        pause_reason = automatic_intent_denial_reason(session, intent) if intent else None
        if pause_reason:
            # Leave the item pending and avoid starving control confirmations
            # behind a paused batch. No claim/network attempt has happened.
            candidate.last_error = pause_reason
            candidate.available_at = now_utc() + timedelta(seconds=5)
            continue
        # Owner cancellation and dispatch use the same lock order: grace first,
        # then outbox. This makes the PENDING -> PROCESSING claim the exact last
        # safe cancellation boundary.
        if candidate.action_type in {"agent_execution_text", "agent_execution_voice"} and candidate.destination == "local_transport":
            grace_allowed = grace_allows_interaction(session, candidate.interaction_id, lock=True)
        else:
            grace_allowed = True
        row_query = select(OutboxMessageRow).where(
            OutboxMessageRow.id == candidate.id,
            OutboxMessageRow.status.in_(["PENDING", "RETRY"]),
            OutboxMessageRow.available_at <= now_utc(),
        )
        if session.bind and session.bind.dialect.name == "postgresql":
            row_query = row_query.with_for_update(skip_locked=True)
        row = session.scalar(row_query.execution_options(populate_existing=True))
        if row is None:
            continue
        if not grace_allowed:
            row.status = "CANCELED"
            row.completed_at = now_utc()
            row.last_error = "CANCELED_BY_HUMAN_REPLY"
            _mark_voice_outbox_artifact_terminal(session, row)
            intent = session.get(AgentExecutionIntentRow, row.execution_intent_id) if row.execution_intent_id else None
            if intent and intent.status != "SENT":
                intent.status = "CANCELLED"
                intent.blocked_reason = "CANCELED_BY_HUMAN_REPLY"
            continue
        row.status = "PROCESSING"
        row.claimed_at = now_utc()
        row.claimed_by = worker
        row.attempt_count = (row.attempt_count or 0) + 1
        audit(session, row.interaction_id, "outbox_claimed", {"outbox_id": row.id, "worker": worker}, row.correlation_id, row.causation_id)
        rows.append(row)
    session.flush()
    return rows


def _is_external_network_outbox(row: OutboxMessageRow) -> bool:
    return bool(
        row.destination in {"wwebjs", "local_transport"}
        or row.action_type
        in {
            "wwebjs_outbound_text",
            "wwebjs_manual_reply_text",
            "agent_execution_text",
            "agent_execution_voice",
        }
    )


def _mark_voice_outbox_artifact_terminal(
    session: Session,
    row: OutboxMessageRow,
    *,
    ambiguous: bool = False,
) -> None:
    if row.action_type != "agent_execution_voice":
        return
    from attention_router.application.voice_tts import mark_voice_outbox_terminal

    mark_voice_outbox_terminal(session, row, ambiguous=ambiguous)


def _mark_external_ambiguous(
    session: Session,
    row: OutboxMessageRow,
    reason: str,
) -> None:
    row.status = "AMBIGUOUS"
    row.last_error = reason
    _mark_voice_outbox_artifact_terminal(session, row, ambiguous=True)
    intent = (
        session.get(AgentExecutionIntentRow, row.execution_intent_id)
        if row.execution_intent_id
        else None
    )
    if intent:
        intent.status = "BLOCKED"
        intent.blocked_reason = reason
    interaction = session.get(InteractionRow, row.interaction_id)
    audit(
        session,
        row.interaction_id,
        "outbox_ambiguous_no_replay",
        {"outbox_id": row.id, "reason": reason},
        row.correlation_id,
        row.causation_id,
    )
    if interaction is None:
        return
    try:
        with session.begin_nested():
            record_finding(
                session,
                FindingCandidate(
                    tenant_id=interaction.tenant_id,
                    category=FindingCategory.BLOCKER,
                    severity=FindingSeverity.HIGH,
                    title="External delivery outcome requires reconciliation",
                    summary="The external delivery outcome is not proven and blind replay is blocked.",
                    component_key="outbox",
                    reason_code=reason,
                    normalized_scope={"outbox_id": row.id},
                    correlation_id=row.correlation_id,
                    provenance="application.services:outbox",
                ),
            )
    except Exception as exc:
        audit(
            session,
            row.interaction_id,
            "outbox_ambiguity_finding_failed",
            {"outbox_id": row.id, "error_class": type(exc).__name__},
            row.correlation_id,
            row.causation_id,
        )


def _mark_external_reconciliation_required(
    session: Session,
    row: OutboxMessageRow,
    reason: str,
) -> None:
    row.status = "RECONCILIATION_REQUIRED"
    row.last_error = reason
    _mark_voice_outbox_artifact_terminal(session, row, ambiguous=True)
    intent = (
        session.get(AgentExecutionIntentRow, row.execution_intent_id)
        if row.execution_intent_id
        else None
    )
    if intent:
        intent.status = "BLOCKED"
        intent.blocked_reason = reason
    audit(
        session,
        row.interaction_id,
        "outbox_sent_finalization_reconciliation_required",
        {"outbox_id": row.id, "reason": reason},
        row.correlation_id,
        row.causation_id,
    )


def process_outbox(session: Session, worker: str | None = None, limit: int = 10, fail: bool = False) -> int:
    worker = worker or worker_id()
    rows = claim_outbox(session, worker, limit)
    # Release claim locks before any provider/browser/network call. Runtime
    # sessions use expire_on_commit=False, so the claimed payload stays local.
    session.commit()
    for row in rows:
        if row.action_type in {"agent_execution_text", "agent_execution_voice"}:
            intent = (
                session.get(AgentExecutionIntentRow, row.execution_intent_id)
                if row.execution_intent_id
                else None
            )
            if intent is None:
                row.status = "BLOCKED"
                row.last_error = "AGENT_EXECUTION_INTENT_MISSING"
                _mark_voice_outbox_artifact_terminal(session, row)
                audit(
                    session,
                    row.interaction_id,
                    "execution.blocked",
                    {
                        "intent_id": row.execution_intent_id,
                        "reason": "AGENT_EXECUTION_INTENT_MISSING",
                    },
                    row.correlation_id,
                    row.causation_id,
                )
                session.commit()
                continue
            if requires_platform_execution_safety(session, intent):
                session.commit()
                transport = probe_transport_status()
                try:
                    if not transport.external_delivery_enabled:
                        raise SafetyDenied("TRANSPORT_EXTERNAL_DELIVERY_DISABLED")
                    validate_platform_outbox_safety(
                        session,
                        intent=intent,
                        outbox=row,
                        transport_ready=transport.ready,
                    )
                    # Locks and reads are released before the network call.  The
                    # durable PROCESSING row prevents a competing dispatcher.
                    session.commit()
                except SafetyDenied as exc:
                    session.rollback()
                    blocked = session.get(OutboxMessageRow, row.id)
                    if blocked is not None:
                        blocked.status = "BLOCKED"
                        blocked.last_error = exc.reason_code
                        _mark_voice_outbox_artifact_terminal(session, blocked)
                        blocked_intent = session.get(
                            AgentExecutionIntentRow,
                            blocked.execution_intent_id,
                        )
                        if blocked_intent:
                            blocked_intent.status = "BLOCKED"
                            blocked_intent.blocked_reason = exc.reason_code
                        audit(
                            session,
                            blocked.interaction_id,
                            "execution.blocked",
                            {
                                "intent_id": blocked.execution_intent_id,
                                "reason": exc.reason_code,
                            },
                        )
                    session.commit()
                    continue
            else:
                session.commit()

        external_attempted = False
        provider_confirmed = False
        try:
            intent = session.get(AgentExecutionIntentRow, row.execution_intent_id) if row.execution_intent_id else None
            if intent and intent.authorization_source == "POLICY_AUTONOMY":
                # Final persistent authority read AFTER claim/safety commits,
                # immediately before adapter dispatch. The shared owner lock
                # stays held until this bounded effect finishes/commits.
                reason = automatic_intent_denial_reason(session, intent, lock=True)
                if reason:
                    row.status = "PENDING"
                    row.claimed_at = None
                    row.claimed_by = None
                    row.attempt_count = max(0, row.attempt_count - 1)
                    row.available_at = now_utc() + timedelta(seconds=5)
                    row.last_error = reason
                    audit(session, row.interaction_id, "execution.deferred",
                          {"outbox_id": row.id, "reason": reason}, origin="owner_control")
                    session.commit()
                    continue
                # Resume is no new grant: normal gates and freshness still win.
                gate, reason, recipient = execution_gate(session, intent, transport_ready=True)
                from attention_router.application.autonomy import _fresh
                decision = session.get(AgentDecisionRow, intent.agent_decision_id)
                event = session.get(InboundEventRow, decision.event_id) if decision else None
                if not settings.autonomous_execution_enabled:
                    gate, reason = "BLOCKED", "AUTONOMOUS_EXECUTION_DISABLED"
                elif event is None or not _fresh(event):
                    gate, reason = "BLOCKED", "STALE_OR_BEFORE_ACTIVATION"
                elif decision.interaction_id != row.interaction_id:
                    gate, reason = "BLOCKED", "OUTBOX_INTERACTION_MISMATCH"
                elif event.source == "wwebjs":
                    if recipient is None or row.payload.get("external_actor_id") != recipient.reference or row.destination != "local_transport":
                        gate, reason = "BLOCKED", "DIRECT_OUTBOX_CONTRACT_MISMATCH"
                    elif row.action_type == "agent_execution_text":
                        if row.payload.get("text") != intent.effective_response_snapshot:
                            gate, reason = "BLOCKED", "DIRECT_OUTBOX_CONTRACT_MISMATCH"
                    elif row.action_type == "agent_execution_voice":
                        try:
                            from attention_router.application.voice_tts import validate_voice_outbox
                            validate_voice_outbox(session, row, intent)
                        except ValueError as exc:
                            gate, reason = "BLOCKED", str(exc)
                    else:
                        gate, reason = "BLOCKED", "DIRECT_OUTBOX_CONTRACT_MISMATCH"
                if gate != "ALLOWED":
                    row.status = "BLOCKED"
                    row.last_error = reason
                    _mark_voice_outbox_artifact_terminal(session, row)
                    intent.status = "BLOCKED"
                    intent.blocked_reason = reason
                    audit(session, row.interaction_id, "execution.blocked",
                          {"outbox_id": row.id, "reason": reason})
                    session.commit()
                    continue
            if fail:
                raise RuntimeError("forced outbox failure")
            if row.destination == "wwebjs" and row.action_type in {"wwebjs_outbound_text", "wwebjs_manual_reply_text"}:
                external_attempted = True
                with start_span("transport.send") as transport_span:
                    result = wwebjs_outbound.dispatch_outbox(row)
                    provider_confirmed = result.status in {"sent", "already_sent"}
                    safe_set_attribute(transport_span, "attention.delivery_type", "wwebjs")
                    set_outcome(transport_span, "SENT")
                if row.action_type == "wwebjs_manual_reply_text":
                    audit(
                        session,
                        row.interaction_id,
                        "manual_reply_delivered",
                        {"outbox_id": row.id, "ha_status": result.status},
                        row.correlation_id,
                        row.causation_id,
                        origin="manual_operator",
                    )
                audit(
                    session,
                    row.interaction_id,
                    "wwebjs_outbound_delivered",
                    {"outbox_id": row.id, "ha_status": result.status},
                    row.correlation_id,
                    row.causation_id,
                )
            elif row.action_type in {"agent_execution_text", "agent_execution_voice"} and row.destination == "local_transport":
                if row.action_type == "agent_execution_voice" and intent:
                    from attention_router.application.voice_tts import validate_voice_outbox
                    validate_voice_outbox(session, row, intent)
                external_attempted = True
                with start_span("transport.send") as transport_span:
                    result = local_transport_outbound.dispatch_outbox(row)
                    provider_confirmed = result.status in {"sent", "already_sent"}
                    safe_set_attribute(transport_span, "attention.delivery_type", "local_transport")
                    safe_set_attribute(transport_span, "attention.message_reference_present", bool((result.response or {}).get("message_reference")))
                    set_outcome(transport_span, "SENT")
                intent = session.get(AgentExecutionIntentRow, row.execution_intent_id)
                if intent:
                    consumption = session.scalar(
                        select(EffectConsumptionRow).where(
                            EffectConsumptionRow.outbox_message_id == row.id
                        )
                    )
                    lease = (
                        session.get(ExecutionLeaseRow, consumption.execution_lease_id)
                        if consumption
                        else None
                    )
                    if consumption and lease:
                        finalize_consumed_in_transaction(
                            session,
                            tenant_id=consumption.tenant_id,
                            lease_id=lease.id,
                            consumption_id=consumption.id,
                            logical_execution_id=lease.logical_execution_id or "",
                            logical_effect_id=consumption.logical_effect_id,
                        )
                    intent.status = "SENT"
                    intent.execution_allowed = True
                    intent.external_delivery_allowed = True
                    audit(session, row.interaction_id, "execution.dispatch_succeeded", {"intent_id": intent.id, "outbox_id": row.id, "message_reference_present": bool((result.response or {}).get("message_reference"))})
                    if row.action_type == "agent_execution_voice":
                        _mark_voice_outbox_artifact_terminal(session, row)
            elif row.action_type == OWNER_CONTROL_OUTBOX_ACTION and row.destination == "local_transport":
                external_attempted = True
                with start_span("transport.send") as transport_span:
                    result = local_transport_outbound.dispatch_outbox(row)
                    provider_confirmed = result.status in {"sent", "already_sent"}
                    safe_set_attribute(transport_span, "attention.delivery_type", "local_transport")
                    safe_set_attribute(
                        transport_span,
                        "attention.message_reference_present",
                        bool((result.response or {}).get("message_reference")),
                    )
                    set_outcome(transport_span, "SENT")
                audit(
                    session,
                    row.interaction_id,
                    "owner_control.confirmation_delivered",
                    {
                        "outbox_id": row.id,
                        "transport_status": result.status,
                        "message_reference_present": bool(
                            (result.response or {}).get("message_reference")
                        ),
                    },
                    row.correlation_id,
                    row.causation_id,
                    origin="owner_control",
                )
            else:
                actions.dispatch(row.destination, row.interaction_id)
            row.status = "DONE"
            row.completed_at = now_utc()
            interaction = session.get(InteractionRow, row.interaction_id)
            intent = session.get(AgentExecutionIntentRow, row.execution_intent_id) if row.execution_intent_id else None
            if interaction:
                try:
                    with session.begin_nested():
                        record_timeline_event(
                            session,
                            tenant_id=interaction.tenant_id,
                            canonical_event_id=intent.canonical_event_id if intent else None,
                            actor_id=interaction.contact_id,
                            event_type="MESSAGE_DELIVERED",
                            occurred_at=row.completed_at,
                            provenance="outbox_delivery",
                            event_ref={"outbox_id": row.id},
                        )
                except Exception as exc:
                    audit(
                        session,
                        row.interaction_id,
                        "timeline_record_failed",
                        {"outbox_id": row.id, "error_class": type(exc).__name__},
                        row.correlation_id,
                        row.causation_id,
                    )
            audit(session, row.interaction_id, "outbox_completed", {"outbox_id": row.id}, row.correlation_id, row.causation_id)
            session.commit()
        except WwebjsOutboundConfigError as exc:
            row.status = "FAILED"
            row.last_error = "TRANSPORT_CONFIGURATION_DENIED"
            _mark_voice_outbox_artifact_terminal(session, row)
            audit(session, row.interaction_id, "outbox_failed", {"outbox_id": row.id, "error_class": type(exc).__name__}, row.correlation_id, row.causation_id)
            if row.execution_intent_id:
                intent = session.get(AgentExecutionIntentRow, row.execution_intent_id)
                if intent:
                    intent.status = "FAILED"
                    audit(session, row.interaction_id, "execution.dispatch_failed", {"intent_id": intent.id, "error_class": type(exc).__name__})
            session.commit()
        except WwebjsOutboundPermanentError:
            _mark_external_ambiguous(
                session,
                row,
                "TRANSPORT_PERMANENT_OUTCOME_UNCERTAIN",
            )
            session.commit()
        except WwebjsOutboundError as exc:
            _mark_external_ambiguous(
                session,
                row,
                "TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS",
            )
            if row.action_type == OWNER_CONTROL_OUTBOX_ACTION:
                audit(
                    session,
                    row.interaction_id,
                    "owner_control.confirmation_delivery_ambiguous",
                    {"outbox_id": row.id, "error_class": type(exc).__name__},
                    row.correlation_id,
                    row.causation_id,
                    origin="owner_control",
                )
            else:
                audit(
                    session,
                    row.interaction_id,
                    "execution.dispatch_failed",
                    {"intent_id": row.execution_intent_id, "error_class": type(exc).__name__},
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            current = session.get(OutboxMessageRow, row.id)
            if current is None:
                raise
            if provider_confirmed:
                _mark_external_reconciliation_required(
                    session,
                    current,
                    "PROVIDER_CONFIRMED_LOCAL_FINALIZATION_FAILED",
                )
            elif external_attempted:
                _mark_external_ambiguous(
                    session,
                    current,
                    "EXTERNAL_REQUEST_OUTCOME_AMBIGUOUS",
                )
            else:
                current.status = "RETRY" if current.attempt_count < 3 else "FAILED"
                current.last_error = type(exc).__name__
                if current.status == "FAILED":
                    _mark_voice_outbox_artifact_terminal(session, current)
                audit(
                    session,
                    current.interaction_id,
                    "outbox_failed",
                    {"outbox_id": current.id, "error_class": type(exc).__name__},
                    current.correlation_id,
                    current.causation_id,
                )
            session.commit()
            if fail:
                raise
    return len(rows)


def force_next_timer(session: Session) -> dict | None:
    timer = session.scalars(
        select(TimerRow).where(TimerRow.status.in_(["pending", "PENDING", "RETRY"])).order_by(TimerRow.due_at).limit(1)
    ).first()
    if not timer:
        return None
    timer.due_at = now_utc()
    session.flush()
    process_due_timers(session, limit=1)
    row = session.get(InteractionRow, timer.interaction_id)
    return interaction_to_dict(session, row)
