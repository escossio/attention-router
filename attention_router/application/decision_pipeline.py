import json
from attention_router.application.direct_conversation import direct_conversation_eligibility, DIRECT_TEXT_CONVERSATION_CONTRACT_VERSION
from attention_router.application.voice_transcription import (
    effective_inbound_text,
    effective_interaction_text,
    is_voice_input_event,
    voice_decision_readiness,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.domain.behavior import (
    ConversationSlots,
    behavior_profile,
    explicit_intent,
    extract_conversation_slots,
    render_response,
)
from attention_router.domain.decision import DecisionContext, DecisionEngine, ResponseGenerator
from attention_router.domain.decision import DecisionResult, DecisionType
from attention_router.application.agents import AndyAgentError, run_andy
from attention_router.application.agents.readiness import get_andy_readiness
from attention_router.application.agents.context import ActionCapability, AllowedAgentContext
from attention_router.application.lab_conversation import claim_inbound
from attention_router.application.platform.capability_pack import execute_owner_capability
from attention_router.application.platform.context import build_context_snapshot, resolve_represented_subject
from attention_router.application.platform.entities import (
    EffectiveRelationship,
    resolve_effective_audience,
    resolve_effective_relationship,
)
from attention_router.core.entities import EntityReference
from attention_router.application.platform.disclosure import evaluate_disclosure_authority, project_private_state_for_agent
from attention_router.application.platform.events import normalize_inbound_event
from attention_router.application.platform.registry import resolve_capability_request
from attention_router.platform.standing_directives import resolve_effective_standing_directives
from attention_router.domain.models import new_id, now_utc
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.policies import resolve_policy
from attention_router.application.memory import memory_context
from attention_router.infrastructure.models import (
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    AgentDecisionRow,
    AgentExecutionIntentRow,
    ActorBindingRow,
    AuditEventRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PolicyVersionRow,
)
from attention_router.infrastructure.repository import (
    get_active_policy_version,
    list_policies,
    mask_identifier,
    resolve_actor_binding,
)
from attention_router.observability.tracing import identifier_hash, safe_set_attribute, set_outcome, start_span


def _audit(session: Session, interaction_id: str, event_type: str, payload: dict[str, Any]) -> None:
    interaction = session.get(InteractionRow, interaction_id)
    session.add(
        AuditEventRow(
            id=new_id(),
            tenant_id=interaction.tenant_id if interaction else DEFAULT_TENANT_ID,
            interaction_id=interaction_id,
            event_type=event_type,
            correlation_id=None,
            causation_id=None,
            origin="agent_decision_pipeline",
            payload=payload,
            created_at=now_utc(),
        )
    )


def _behavior_history(
    session: Session,
    tenant_id: str,
    contact_id: str,
) -> tuple[bool, set[str], ConversationSlots | None]:
    interactions = session.scalars(
        select(InteractionRow)
        .where(
            InteractionRow.tenant_id == tenant_id,
            InteractionRow.contact_id == contact_id,
        )
        .order_by(InteractionRow.created_at.desc())
        .limit(5)
    ).all()
    if not interactions:
        return False, set(), None
    ids = [item.id for item in interactions]
    audits = session.scalars(
        select(AuditEventRow)
        .where(
            AuditEventRow.tenant_id == tenant_id,
            AuditEventRow.interaction_id.in_(ids),
            AuditEventRow.event_type == "behavior.response_generated",
        )
        .order_by(AuditEventRow.created_at.desc())
        .limit(5)
    ).all()
    slots = None
    for item in reversed(interactions):
        slots = extract_conversation_slots(item.inbound_text, slots)
    return (
        any(bool(item.payload.get("introduction_included")) for item in audits),
        {str(item.payload.get("variant_id")) for item in audits if item.payload.get("variant_id")},
        slots,
    )


def _blueprint(
    session: Session,
    binding: ActorBindingRow | None,
    tenant_id: str,
    *, explicit_only: bool = False,
) -> tuple[AgentBlueprintRow | None, AgentBlueprintVersionRow | None]:
    blueprint_id = (binding.binding_metadata or {}).get("agent_blueprint_id") if binding else None
    blueprint_id = blueprint_id or settings.agent_decision_default_blueprint_id
    row = session.get(AgentBlueprintRow, blueprint_id) if blueprint_id else None
    if row is not None and row.tenant_id != tenant_id:
        row = None
    if row is None and not explicit_only:
        candidates = session.scalars(
            select(AgentBlueprintRow).where(
                AgentBlueprintRow.tenant_id == tenant_id,
                AgentBlueprintRow.current_version_id.is_not(None),
            )
        ).all()
        ranked = []
        status_rank = {"published": 3, "ready_for_simulation": 2, "draft": 1}
        for candidate in candidates:
            version = session.get(AgentBlueprintVersionRow, candidate.current_version_id)
            if version is not None:
                completeness = int((version.spec or {}).get("configuration_completeness", 0))
                ranked.append((status_rank.get(candidate.status, 0), completeness, candidate.updated_at, candidate))
        row = max(ranked, key=lambda item: item[:3])[3] if ranked else None
    if row is None or not row.current_version_id:
        return row, None
    version = session.get(AgentBlueprintVersionRow, row.current_version_id)
    if version is None or version.blueprint_id != row.id:
        return row, None
    return row, version


def _policy(session: Session, interaction: InteractionRow, binding: ActorBindingRow | None, audience: str) -> PolicyVersionRow | None:
    try:
        resolution = resolve_policy(
            list_policies(session, interaction.tenant_id),
            binding.actor_key if binding else interaction.contact_id,
            interaction.relationship_category,
            interaction.active_context,
            audience=audience,
            binding_id=binding.id if binding else None,
        )
        return get_active_policy_version(session, resolution.winner.identifier, interaction.tenant_id)
    except (KeyError, ValueError):
        return None


def _policy_resolution(session: Session, interaction: InteractionRow, binding: ActorBindingRow | None, audience: str):
    """Return the resolver evidence and active version used by the decision."""
    try:
        resolution = resolve_policy(
            list_policies(session, interaction.tenant_id),
            binding.actor_key if binding else interaction.contact_id,
            interaction.relationship_category,
            interaction.active_context,
            audience=audience,
            binding_id=binding.id if binding else None,
        )
        return resolution, get_active_policy_version(
            session, resolution.winner.identifier, interaction.tenant_id
        )
    except (KeyError, ValueError):
        return None, None


@dataclass(frozen=True, slots=True)
class DecisionRoutingContext:
    binding: ActorBindingRow | None
    lookup_source: str
    lookup_identifier: str
    relationship: EffectiveRelationship
    audience_resolution: Any
    audience: str
    policy_resolution: Any
    policy_version: PolicyVersionRow | None
    represented_subject: EntityReference | None


def resolve_decision_routing(
    session: Session,
    event: InboundEventRow,
    interaction: InteractionRow,
) -> DecisionRoutingContext:
    """Resolve the canonical routing prefix without invoking Andy or mutating it."""
    lookup_source = event.source
    lookup_identifier = str(event.payload.get("actor_id", interaction.contact_id))
    binding = resolve_actor_binding(
        session, lookup_source, lookup_identifier, interaction.tenant_id
    )
    represented_subject = resolve_represented_subject(session, interaction.tenant_id)
    persisted_relationship = (
        resolve_effective_relationship(
            session,
            tenant_id=interaction.tenant_id,
            source=EntityReference(entity_type="ACTOR", entity_id=binding.actor_key),
            target=represented_subject,
        )
        if binding and represented_subject else None
    )
    relationship = persisted_relationship or EffectiveRelationship(
        relationship_id=None,
        relationship_type=interaction.relationship_category,
        state="READY" if interaction.relationship_category else "UNKNOWN",
        reason_code=(
            "EVENT_RELATIONSHIP_CONTEXT"
            if interaction.relationship_category else "RELATIONSHIP_CONTEXT_MISSING"
        ),
    )
    audience_resolution = resolve_effective_audience(actor=binding, relationship=relationship)
    audience = audience_resolution.audience or interaction.relationship_category or "unknown"
    policy_resolution, policy_version = _policy_resolution(
        session, interaction, binding, audience
    )
    return DecisionRoutingContext(
        binding=binding,
        lookup_source=lookup_source,
        lookup_identifier=lookup_identifier,
        relationship=relationship,
        audience_resolution=audience_resolution,
        audience=audience,
        policy_resolution=policy_resolution,
        policy_version=policy_version,
        represented_subject=represented_subject,
    )


def _live_behavior_profile(binding: ActorBindingRow | None) -> dict[str, Any] | None:
    """Load behavior only when the explicit, binding-scoped gate is enabled."""
    if not settings.andy_behavior_enabled or not settings.andy_behavior_canary_binding_id:
        return None
    if binding is None or binding.id != settings.andy_behavior_canary_binding_id:
        return None
    try:
        profile = json.loads(Path(settings.andy_behavior_profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(profile, dict):
        return None
    return behavior_profile({"behavior": profile}, allow_disabled=True)


def _actor_memory_context(
    session: Session,
    actor_key: str,
    binding: ActorBindingRow | None,
    tenant_id: str,
) -> dict[str, Any] | None:
    if not settings.memory_context_enabled:
        return None
    if settings.persistent_memory_canary_binding_id and (binding is None or binding.id != settings.persistent_memory_canary_binding_id):
        return None
    from attention_router.infrastructure.models import MemoryActorRow
    actor = session.scalar(
        select(MemoryActorRow).where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
    )
    return memory_context(session, actor.id, tenant_id) if actor else None


def _agent_enabled_for(
    binding: ActorBindingRow | None,
    audience: str,
    policy_version: PolicyVersionRow | None,
    owner_authenticated: bool = False,
    *, event: InboundEventRow | None = None, blueprint_configured: bool = False,
) -> bool:
    if get_andy_readiness().state != "READY":
        return False
    if not settings.andy_agent_enabled:
        return False
    if owner_authenticated:
        return True
    return blueprint_configured and direct_conversation_eligibility(event).eligible


def _recent_agent_turns(
    session: Session,
    tenant_id: str,
    contact_id: str,
    current_id: str,
) -> list[dict[str, str]]:
    current = session.get(InteractionRow, current_id)
    if current is None or current.tenant_id != tenant_id or current.contact_id != contact_id:
        return []
    rows = session.scalars(
        select(InteractionRow)
        .where(
            InteractionRow.tenant_id == tenant_id,
            InteractionRow.contact_id == contact_id,
            InteractionRow.created_at < current.created_at,
        )
        .order_by(InteractionRow.created_at.desc(), InteractionRow.id.desc())
        .limit(6)
    ).all()
    turns = []
    for row in reversed(rows):
        content = effective_interaction_text(session, row)
        if content:
            turns.append({"role": "user", "content": content})
        assistant_text = session.scalar(
            select(AgentExecutionIntentRow.effective_response_snapshot)
            .join(
                AgentDecisionRow,
                AgentDecisionRow.id == AgentExecutionIntentRow.agent_decision_id,
            )
            .join(
                OutboxMessageRow,
                OutboxMessageRow.execution_intent_id == AgentExecutionIntentRow.id,
            )
            .where(
                AgentDecisionRow.interaction_id == row.id,
                OutboxMessageRow.interaction_id == row.id,
                OutboxMessageRow.status == "DONE",
                OutboxMessageRow.destination == "local_transport",
                OutboxMessageRow.action_type.in_(
                    ("agent_execution_text", "agent_execution_voice")
                ),
            )
            .order_by(OutboxMessageRow.created_at.desc(), OutboxMessageRow.id.desc())
            .limit(1)
        )
        if assistant_text and assistant_text.strip():
            turns.append({"role": "assistant", "content": assistant_text})
    return turns


def process_agent_decision(session: Session, event_id: str) -> AgentDecisionRow | None:
    existing = session.scalars(
        select(AgentDecisionRow).where(
            AgentDecisionRow.event_id == event_id,
            AgentDecisionRow.decision_pipeline_version == settings.agent_decision_pipeline_version,
        )
    ).first()
    if existing:
        return existing
    event = session.get(InboundEventRow, event_id)
    if event is None or event.interaction_id is None:
        return None
    interaction = session.get(InteractionRow, event.interaction_id)
    if interaction is None:
        return None
    voice_state, voice_reason = voice_decision_readiness(session, event)
    if is_voice_input_event(event) and voice_state != "READY":
        already_audited = session.scalar(
            select(AuditEventRow.id).where(
                AuditEventRow.tenant_id == interaction.tenant_id,
                AuditEventRow.interaction_id == interaction.id,
                AuditEventRow.event_type == "voice.transcription_required",
            )
        )
        if already_audited is None:
            _audit(
                session,
                interaction.id,
                "voice.transcription_required",
                {"reason_code": voice_reason},
            )
            session.flush()
        return None
    effective_text = effective_inbound_text(session, event, interaction)
    canonical_event = normalize_inbound_event(session, event, actor_id=interaction.contact_id)
    _audit(session, interaction.id, "decision.started", {"pipeline_version": settings.agent_decision_pipeline_version})
    routing = resolve_decision_routing(session, event, interaction)
    binding = routing.binding
    audience = routing.audience
    resolution = routing.policy_resolution
    policy_version = routing.policy_version
    with start_span("actor.resolve") as actor_span:
        lookup_source = routing.lookup_source
        lookup_identifier = routing.lookup_identifier
        lookup_identifier_type = "lid" if str(lookup_identifier).casefold().endswith("@lid") else (
            "jid" if "@" in str(lookup_identifier) else "actor_key"
        )
        binding = resolve_actor_binding(
            session, lookup_source, lookup_identifier, interaction.tenant_id
        )
        safe_set_attribute(actor_span, "attention.binding_resolved", bool(binding))
        safe_set_attribute(actor_span, "attention.actor_resolution", "BOUND" if binding else "UNKNOWN")
        safe_set_attribute(actor_span, "attention.binding.lookup_source", lookup_source)
        safe_set_attribute(actor_span, "attention.binding.lookup_identifier_type", lookup_identifier_type)
        safe_set_attribute(actor_span, "attention.binding.lookup_identifier_hash", identifier_hash(lookup_identifier))
        safe_set_attribute(actor_span, "attention.binding.match_found", bool(binding))
        safe_set_attribute(actor_span, "attention.binding.resolution_reason", "EXACT_SOURCE_IDENTIFIER" if binding else "NO_ACTIVE_EXACT_MATCH")
        safe_set_attribute(actor_span, "attention.binding_id", identifier_hash(binding.id) if binding else None)
        safe_set_attribute(actor_span, "attention.binding.actor_id", identifier_hash(binding.actor_key) if binding else None)
    if binding:
        _audit(session, interaction.id, "actor.resolved", {"status": "BOUND", "binding_present": True})
    else:
        _audit(session, interaction.id, "actor.resolved", {"status": "UNKNOWN", "binding_present": False})
    direct = direct_conversation_eligibility(event)
    blueprint, version = _blueprint(session, binding, interaction.tenant_id, explicit_only=direct.eligible)
    if direct.eligible and version is None:
        _audit(session, interaction.id, "conversation.blocked",
               {"reason_code": "CONVERSATION_BLUEPRINT_UNCONFIGURED"})
    if blueprint and version:
        _audit(session, interaction.id, "agent.resolved", {"status": "BOUND", "version": version.version})
    else:
        _audit(session, interaction.id, "agent.resolved", {"status": "AGENT_NOT_CONFIGURED"})
    with start_span("policy.resolve") as policy_span:
        safe_set_attribute(policy_span, "attention.binding_resolved", bool(binding))
        safe_set_attribute(policy_span, "attention.policy_candidate_count", len(resolution.matched) if resolution else 0)
        safe_set_attribute(policy_span, "attention.selected_policy_id", resolution.winner.identifier if resolution else None)
        safe_set_attribute(policy_span, "attention.selected_policy_specificity", resolution.matched[0]["specificity"] if resolution else None)
        safe_set_attribute(policy_span, "attention.selected_policy_version_id", policy_version.id if policy_version else None)
        set_outcome(policy_span, "SELECTED" if policy_version else "FALLBACK_UNAVAILABLE")
    _audit(session, interaction.id, "audience.resolved", {"status": "RESOLVED", "audience": audience})
    policy_config = policy_version.config if policy_version else None
    if policy_version:
        # Keep the interaction's resolved policy in sync with the worker-owned
        # decision path; the DecisionEngine uses this as its policy contract.
        interaction.policy_id = resolution.winner.identifier
        interaction.policy_version_id = policy_version.id
        _audit(session, interaction.id, "policy.selected", {"policy_version_id": policy_version.id})
    claim_inbound(session, event, binding.id if binding else None, policy_version.policy_id if policy_version else None)
    context = DecisionContext(
        event_id=event.id,
        interaction_id=interaction.id,
        channel=event.payload.get("channel", event.source),
        source=event.source,
        actor_id=interaction.contact_id,
        actor_binding_id=binding.id if binding else None,
        actor_category=interaction.relationship_category,
        actor_metadata=binding.binding_metadata if binding else {},
        agent_blueprint_id=blueprint.id if blueprint and version else None,
        agent_blueprint_version=version.version if version else None,
        agent_spec=version.spec if version else None,
        audience=audience,
        policy_id=interaction.policy_id if policy_version else None,
        policy_version_id=policy_version.id if policy_version else None,
        policy_config=policy_config,
        autonomy=((version.spec if version else {}).get("autonomy") or {}).get("level", "observe"),
        inbound_text=effective_text,
    )
    agent_output = None
    agent_result = None
    capability_resolutions: list[dict[str, Any]] = []
    owner_authenticated = (
        event.payload.get("event_origin") == "OWNER_COMMAND"
        and event.payload.get("owner_authenticated") is True
    )
    agent_path_enabled = _agent_enabled_for(
        binding, audience, policy_version, owner_authenticated,
        event=event, blueprint_configured=version is not None,
    )
    if agent_path_enabled:
        represented_subject = resolve_represented_subject(session, interaction.tenant_id)
        platform_context = build_context_snapshot(
            session,
            canonical_event,
            represented_subject=represented_subject,
        )
        standing_directives = resolve_effective_standing_directives(
            session,
            tenant_id=interaction.tenant_id,
            subject_actor_id=represented_subject.entity_id if represented_subject else "",
            trigger_type="INBOUND_MESSAGE",
            audience=audience,
            relationship=interaction.relationship_category,
        ) if represented_subject else []
        directive_context = [
            {
                "id": directive.id,
                "trigger_type": directive.trigger_type,
                "effect_type": directive.effect_type,
                "audience_type": (directive.audience_selector or {}).get("type", ""),
            }
            for directive in standing_directives
        ]
        allowed_disclosures = list((policy_config or {}).get("allowed_disclosures") or [])
        disclosure_authority = evaluate_disclosure_authority(
            directives=standing_directives,
            allowed_disclosures=allowed_disclosures,
            operational_state=platform_context.current_operational_state,
        )
        disclose_presence = disclosure_authority.allowed
        private_state = project_private_state_for_agent(disclosure_authority, platform_context.current_operational_state)
        _audit(session, interaction.id, "agent_context.private_state_exposed", {
            "private_state_exposed": bool(private_state),
            "disclosure_reason_code": disclosure_authority.reason_code,
        })
        agent_context = AllowedAgentContext(
            actor_id=identifier_hash(context.actor_id),
            binding_id=identifier_hash(context.actor_binding_id) if context.actor_binding_id else None,
            interaction_actor={"type": "ACTOR", "id": identifier_hash(context.actor_id)},
            represented_subject=represented_subject.model_dump() if represented_subject else None,
            audience=audience,
            policy_summary=f"policy={policy_version.policy_id if policy_version else 'none'}; "
            f"conversation_contract={DIRECT_TEXT_CONVERSATION_CONTRACT_VERSION}; capabilities_require_separate_authority",
            allowed_disclosures=allowed_disclosures,
            allowed_facts=[],
            recent_turns=_recent_agent_turns(
                session, interaction.tenant_id, interaction.contact_id, interaction.id
            ),
            available_action_capabilities=["leave_message", "request_callback", "notify_alex"],
            current_message=effective_text,
            relationship=interaction.relationship_category,
            contact_return_channel_available=direct.return_channel_available,
            whatsapp_return_channel_available=direct.return_channel_available,
            already_known_information=(["actor_identity", "relationship"] if binding else [])
            + (["current_whatsapp_return_channel"] if direct.return_channel_available else []),
            action_capabilities=[
                ActionCapability(
                    action_type="leave_message",
                    known_channel="current_whatsapp_return_channel",
                    description="propose registering a message for Router review; does not send it",
                ),
                ActionCapability(
                    action_type="request_callback",
                    optional_parameters=["callback_number"],
                    known_channel="current_whatsapp_return_channel",
                    description="propose a callback request; no phone call is executed by Andy",
                ),
                ActionCapability(
                    action_type="notify_alex",
                    available=False,
                    description="no direct notification capability is exposed to this run",
                ),
            ],
            available_capabilities=platform_context.available_capabilities,
            current_operational_state=private_state,
            relevant_facts=platform_context.relevant_facts,
            relevant_decisions=platform_context.relevant_decisions,
            effective_authority=platform_context.effective_authority,
            effective_standing_directives=directive_context,
            communication_intent={
                "disclose_current_availability_when_relevant": disclose_presence,
            },
        )
        with start_span("andy.agent.context_build") as span:
            safe_set_attribute(span, "attention.agent.context_turn_count", len(agent_context.recent_turns))
            safe_set_attribute(span, "attention.agent.known_required_information_count", len(agent_context.already_known_information))
            safe_set_attribute(span, "attention.agent.missing_information_count", 0)
            safe_set_attribute(span, "attention.agent.available_action_count", sum(item.available for item in agent_context.action_capabilities))
            safe_set_attribute(span, "attention.agent.requested_action_count", 0)
            safe_set_attribute(span, "attention.context.interaction_actor_resolved", True)
            safe_set_attribute(span, "attention.context.represented_subject_resolved", represented_subject is not None)
            safe_set_attribute(span, "attention.context.represented_state_count", len(private_state))
            safe_set_attribute(
                span,
                "attention.context.presence_effective",
                any(item.get("namespace") == "presence" for item in private_state),
            )
            set_outcome(span, "BUILT")
        _audit(
            session,
            interaction.id,
            "agent_context_built",
            {
                "interaction_actor_resolved": agent_context.interaction_actor is not None,
                "represented_subject_resolved": agent_context.represented_subject is not None,
                "represented_state_count": len(private_state),
                "presence_effective": any(
                    item.get("namespace") == "presence" for item in private_state
                ),
                "context_builder_version": "platform_context_snapshot:represented_subject:v1",
            },
        )
        try:
            with start_span("andy.agent.run") as span:
                agent_result = run_andy(agent_context)
                agent_output = agent_result.output
                safe_set_attribute(span, "attention.agent.model", settings.andy_agent_model)
                safe_set_attribute(span, "attention.agent.intent", agent_output.intent)
                safe_set_attribute(span, "attention.agent.objective", agent_output.objective)
                safe_set_attribute(span, "attention.agent.confidence", agent_output.confidence)
                safe_set_attribute(span, "attention.agent.requested_action_count", len(agent_output.requested_actions))
                safe_set_attribute(span, "attention.agent.requested_capability_count", len(agent_output.requested_capabilities))
                safe_set_attribute(span, "attention.agent.turn_count", agent_result.turn_count)
                set_outcome(span, "COMPLETED")
            configured_capabilities = set((policy_config or {}).get("allowed_capabilities") or [])
            configured_capabilities.update((policy_config or {}).get("allowed_actions") or [])
            for capability_request in agent_output.requested_capabilities:
                if capability_request.capability == "presence.set":
                    normalized_parameters = dict(capability_request.parameters)
                    normalized_parameters.setdefault("state", normalized_parameters.get("status"))
                    normalized_parameters.setdefault("audience_scope", normalized_parameters.get("audience", "all"))
                    capability_request = capability_request.model_copy(update={"parameters": normalized_parameters})
                resource_id = (
                    str(capability_request.resource.get("id"))
                    if capability_request.resource and capability_request.resource.get("id")
                    else None
                )
                resolution = resolve_capability_request(
                    session,
                    capability_request,
                    tenant_id=interaction.tenant_id,
                    grantee_type="ACTOR",
                    grantee_id=binding.actor_key if binding else interaction.contact_id,
                    policy_allows=(
                        capability_request.capability in configured_capabilities
                        or "*" in configured_capabilities
                    ),
                    resource_id=resource_id,
                    owner_authorized=owner_authenticated,
                )
                capability_resolutions.append({
                    "capability": resolution.capability,
                    "status": resolution.status.value,
                    "reason_code": resolution.reason_code,
                })
                if owner_authenticated and resolution.execution_allowed and binding is not None:
                    execution = execute_owner_capability(
                        session,
                        tenant_id=interaction.tenant_id,
                        actor_id=binding.actor_key,
                        request=capability_request,
                        correlation_id=interaction.correlation_id,
                        causation_id=event.id,
                    )
                    capability_resolutions[-1].update(
                        {
                            "execution_status": execution.status,
                            "execution_reason_code": execution.reason_code,
                        }
                    )
        except AndyAgentError as exc:
            with start_span("andy.agent.validate") as span:
                safe_set_attribute(span, "attention.agent.failure_reason", str(exc))
                set_outcome(span, "HOLD")
            _audit(session, interaction.id, "agent.failed", {"reason_code": str(exc)})
            agent_output = None
            agent_result = None
    with start_span("decision.evaluate") as decision_span:
        if agent_output is not None and (agent_output.conversation_state == "hold" or not agent_output.response_text.strip()):
            result = DecisionResult(DecisionType.INSUFFICIENT_CONTEXT, "observe", None, False, "AUTOMATIC_RESPONSE_EMPTY", 0.0, [], "AUTOMATIC_RESPONSE_EMPTY", False, False)
        elif agent_output is not None:
            missing = list(agent_output.missing_information)
            action = "request_information" if agent_output.needs_more_information else "respond"
            decision_kind = DecisionType.REQUEST_INFORMATION if missing else DecisionType.RESPOND
            result = DecisionResult(
                decision_kind, action, agent_output.response_text or None, False, None,
                {"high": 0.9, "medium": 0.65, "low": 0.35}[agent_output.confidence],
                missing, agent_output.reason_code, False, False, agent_output.intent,
                agent_output.objective, not missing, not missing, [],
            )
            safe_set_attribute(decision_span, "attention.semantic_source", "OPENAI_AGENTS_SDK")
        elif agent_path_enabled:
            result = DecisionResult(DecisionType.INSUFFICIENT_CONTEXT, "observe", None, False, "AGENT_FAILURE", 0.0, [], "AGENT_FAILURE", False, False)
            safe_set_attribute(decision_span, "attention.semantic_source", "OPENAI_AGENTS_SDK")
        else:
            result = DecisionEngine().decide(context)
        safe_set_attribute(decision_span, "attention.decision_type", result.decision_type.value)
        safe_set_attribute(decision_span, "attention.recommended_action", result.recommended_action)
        safe_set_attribute(decision_span, "attention.decision.intent", result.intent)
        safe_set_attribute(decision_span, "attention.decision.objective", result.objective)
        safe_set_attribute(decision_span, "attention.decision.self_contained", result.self_contained)
        safe_set_attribute(decision_span, "attention.decision.context_sufficient", result.context_sufficient)
        safe_set_attribute(decision_span, "attention.missing_information_count", len(result.missing_information))
        safe_set_attribute(decision_span, "attention.decision.action", result.recommended_action)
        safe_set_attribute(decision_span, "attention.decision.reason", result.reasoning_summary)
        safe_set_attribute(decision_span, "attention.confidence", result.confidence)
        safe_set_attribute(
            decision_span,
            "attention.response_objective",
            result.objective,
        )
        set_outcome(decision_span, "EVALUATED")
    behavior = None if agent_path_enabled else _live_behavior_profile(binding)
    candidate = None
    with start_span("behavior.generate") as behavior_span:
        if agent_output is not None:
            proposed_response = agent_output.response_text if agent_output.conversation_state != "hold" and agent_output.response_text.strip() else None
            safe_set_attribute(behavior_span, "attention.response_source", "OPENAI_AGENTS_SDK")
            safe_set_attribute(behavior_span, "attention.behavior_branch", agent_output.conversation_state.upper())
            safe_set_attribute(behavior_span, "attention.response_objective", agent_output.objective)
            safe_set_attribute(behavior_span, "attention.behavior_intent", agent_output.intent)
            safe_set_attribute(behavior_span, "attention.agent_output_valid", True)
        elif agent_path_enabled:
            proposed_response = None
            safe_set_attribute(behavior_span, "attention.response_source", "agent_failure")
        elif behavior:
            introduced, recent_variants, known_slots = _behavior_history(
                session, interaction.tenant_id, interaction.contact_id
            )
            actor_memory = _actor_memory_context(
                session, interaction.contact_id, binding, interaction.tenant_id
            )
            text = effective_text.lower()
            urgent = any(marker in text for marker in behavior.get("urgency_markers", []))
            candidate = render_response(
                result,
                behavior,
                audience=audience,
                introduced=introduced,
                recent_variant_ids=recent_variants,
                urgent=urgent,
                inbound_text=effective_text,
                missing_information=result.missing_information,
                known_slots=known_slots,
                memory_context=actor_memory,
            )
            proposed_response = candidate.text if candidate else None
            safe_set_attribute(behavior_span, "attention.response_source", "andy_behavior")
            safe_set_attribute(behavior_span, "attention.behavior_enabled", True)
            safe_set_attribute(behavior_span, "attention.message_family", candidate.message_family if candidate else None)
            behavior_intent = explicit_intent(effective_text, known_slots)
            safe_set_attribute(behavior_span, "attention.behavior_intent", behavior_intent)
            safe_set_attribute(
                behavior_span,
                "attention.behavior_branch",
                "IDENTITY" if behavior_intent == "IDENTITY_QUESTION" else candidate.message_family if candidate else None,
            )
            safe_set_attribute(behavior_span, "attention.response_variant", candidate.variant_id if candidate else None)
            safe_set_attribute(
                behavior_span,
                "attention.response_objective",
                {
                    "IDENTITY_QUESTION": "identity/name",
                    "ASSISTANT_NATURE_QUESTION": "identity/transparency",
                }.get(behavior_intent),
            )
        elif settings.legacy_external_fallback_enabled:
            proposed_response = ResponseGenerator().propose(result)
            safe_set_attribute(behavior_span, "attention.response_source", "legacy")
            safe_set_attribute(behavior_span, "attention.behavior_enabled", False)
        else:
            proposed_response = None
            safe_set_attribute(behavior_span, "attention.response_source", "none")
            safe_set_attribute(behavior_span, "attention.behavior_enabled", False)
            safe_set_attribute(behavior_span, "attention.behavior_failure_reason", "BEHAVIOR_UNAVAILABLE_NO_SAFE_RESPONSE")
            _audit(session, interaction.id, "behavior.unavailable", {"reason": "BEHAVIOR_UNAVAILABLE_NO_SAFE_RESPONSE"})
        safe_set_attribute(behavior_span, "attention.response_text_present", bool(proposed_response))
        safe_set_attribute(behavior_span, "attention.response_text_length", len(proposed_response or ""))
        set_outcome(behavior_span, "GENERATED" if proposed_response else "NO_RESPONSE")
    row = AgentDecisionRow(
        id=new_id(),
        event_id=event.id,
        interaction_id=interaction.id,
        agent_blueprint_id=context.agent_blueprint_id,
        agent_blueprint_version=context.agent_blueprint_version,
        actor_id=context.actor_id,
        actor_binding_id=context.actor_binding_id,
        audience=context.audience,
        policy_version_id=context.policy_version_id,
        decision_pipeline_version=settings.agent_decision_pipeline_version,
        decision_type=result.decision_type.value,
        recommended_action=result.recommended_action,
        proposed_response=proposed_response,
        response_message_family=candidate.message_family if candidate else None,
        response_variant_id=candidate.variant_id if candidate else None,
        response_spoken_text=candidate.spoken_text if candidate else None,
        response_introduction_included=candidate.introduction_included if candidate else None,
        escalation_required=result.escalation_required,
        escalation_reason=result.escalation_reason,
        confidence=result.confidence,
        missing_information=result.missing_information,
        intent=result.intent,
        objective=result.objective,
        self_contained=result.self_contained,
        context_sufficient=result.context_sufficient,
        context_requirements=result.context_requirements or [],
        requested_capabilities=(
            [item.model_dump(mode="json") for item in agent_output.requested_capabilities]
            if agent_output is not None else []
        ),
        canonical_event_id=canonical_event.id,
        semantic_source="OPENAI_AGENTS_SDK" if agent_output is not None else "DETERMINISTIC",
        response_source="OPENAI_AGENTS_SDK" if agent_output is not None else ("ANDY_BEHAVIOR" if behavior else "NONE"),
        execution_allowed=False,
        external_delivery_allowed=False,
        reasoning_summary=result.reasoning_summary,
        status="DRY_RUN",
        created_at=now_utc(),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return session.scalars(
            select(AgentDecisionRow).where(
                AgentDecisionRow.event_id == event.id,
                AgentDecisionRow.decision_pipeline_version == settings.agent_decision_pipeline_version,
            )
        ).one()
    _audit(
        session,
        interaction.id,
        "decision.generated",
        {
            "decision_type": row.decision_type,
            "execution_allowed": False,
            "intent": result.intent,
            "objective": result.objective,
            "self_contained": result.self_contained,
            "context_sufficient": result.context_sufficient,
            "missing_information_count": len(result.missing_information),
            "action": result.recommended_action,
        },
    )
    _audit(session, interaction.id, "decision.persisted", {"decision_id": row.id})
    if capability_resolutions:
        _audit(
            session,
            interaction.id,
            "capability.requests_evaluated",
            {"count": len(capability_resolutions), "resolutions": capability_resolutions},
        )
    if candidate:
        _audit(
            session,
            interaction.id,
            "behavior.response_generated",
            {
                "message_family": candidate.message_family,
                "variant_id": candidate.variant_id,
                "introduction_included": candidate.introduction_included,
                "spoken_text_present": bool(candidate.spoken_text),
                "text_length": len(candidate.text),
            },
        )
    return row


def decision_to_dict(row: AgentDecisionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "interaction_id": row.interaction_id,
        "agent_blueprint_id": row.agent_blueprint_id,
        "agent_blueprint_version": row.agent_blueprint_version,
        "actor_id": mask_identifier(row.actor_id) if row.actor_id else None,
        "audience": row.audience,
        "policy_version_id": row.policy_version_id,
        "decision_pipeline_version": row.decision_pipeline_version,
        "decision_type": row.decision_type,
        "recommended_action": row.recommended_action,
        "proposed_response_present": row.proposed_response is not None,
        "response_message_family": row.response_message_family,
        "response_variant_id": row.response_variant_id,
        "response_spoken_text": row.response_spoken_text,
        "response_introduction_included": row.response_introduction_included,
        "escalation_required": row.escalation_required,
        "escalation_reason": row.escalation_reason,
        "confidence": row.confidence,
        "missing_information": row.missing_information,
        "intent": row.intent,
        "objective": row.objective,
        "self_contained": row.self_contained,
        "context_sufficient": row.context_sufficient,
        "context_requirements": row.context_requirements,
        "requested_capabilities": row.requested_capabilities,
        "canonical_event_id": row.canonical_event_id,
        "semantic_source": row.semantic_source,
        "execution_allowed": row.execution_allowed,
        "external_delivery_allowed": row.external_delivery_allowed,
        "reasoning_summary": row.reasoning_summary,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }
