from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.enums import ActionState, InteractionState, TimerKind
from attention_router.domain.models import ContactIdentity, Interaction, Policy, new_id, now_utc
from attention_router.domain.policies import DEMO_POLICIES
from attention_router.core.tenancy import DEFAULT_TENANT_ID, DEFAULT_TENANT_SLUG
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActionAttemptRow,
    ActorBindingRow,
    AuditEventRow,
    DecisionRow,
    InboundEventRow,
    InteractionRow,
    OutboxMessageRow,
    PolicyRow,
    PolicyVersionRow,
    QueueRow,
    TimerRow,
    TenantRow,
)


def policy_to_config(policy: Policy) -> dict[str, Any]:
    return policy.__dict__.copy()


def row_to_policy(row: PolicyRow) -> Policy:
    return Policy(
        identifier=row.identifier,
        name=row.name,
        match_criteria=row.match_criteria,
        priority=row.priority,
        specificity=row.specificity,
        tone=row.tone,
        initial_wait_seconds=row.initial_wait_seconds,
        allowed_disclosures=row.allowed_disclosures,
        allowed_actions=row.allowed_actions,
        escalation_steps=row.escalation_steps,
        ack_timeout_seconds=row.ack_timeout_seconds,
        repetition_limit=row.repetition_limit,
        cancellation_conditions=row.cancellation_conditions,
        completion_conditions=row.completion_conditions,
    )


def policy_from_version(row: PolicyVersionRow) -> Policy:
    cfg = row.config
    return Policy(
        identifier=cfg["identifier"],
        name=cfg["name"],
        match_criteria=cfg["match_criteria"],
        priority=cfg["priority"],
        specificity=cfg["specificity"],
        tone=cfg["tone"],
        initial_wait_seconds=cfg["initial_wait_seconds"],
        allowed_disclosures=cfg["allowed_disclosures"],
        allowed_actions=cfg["allowed_actions"],
        escalation_steps=cfg["escalation_steps"],
        ack_timeout_seconds=cfg["ack_timeout_seconds"],
        repetition_limit=cfg["repetition_limit"],
        cancellation_conditions=cfg["cancellation_conditions"],
        completion_conditions=cfg["completion_conditions"],
    )


def audit(
    session: Session,
    interaction_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    causation_id: str | None = None,
    previous_state: str | None = None,
    next_state: str | None = None,
    policy_version_id: str | None = None,
    origin: str = "core",
    tenant_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    if tenant_id is None and interaction_id:
        interaction = session.get(InteractionRow, interaction_id)
        tenant_id = interaction.tenant_id if interaction else None
    tenant_id = tenant_id or DEFAULT_TENANT_ID
    session.add(
        AuditEventRow(
            id=new_id(),
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            event_type=event_type,
            correlation_id=correlation_id,
            causation_id=causation_id,
            previous_state=previous_state,
            next_state=next_state,
            policy_version_id=policy_version_id,
            origin=origin,
            payload=payload,
            created_at=created_at or now_utc(),
        )
    )


def ensure_policy_version(
    session: Session, row: PolicyRow, config: dict[str, Any], origin: str
) -> PolicyVersionRow:
    checksum = stable_hash(config)
    existing = session.scalars(
        select(PolicyVersionRow).where(
            PolicyVersionRow.policy_id == row.identifier, PolicyVersionRow.checksum == checksum
        )
    ).first()
    if existing:
        if not row.current_version_id:
            row.current_version_id = existing.id
        return existing
    latest = session.scalars(
        select(PolicyVersionRow)
        .where(PolicyVersionRow.policy_id == row.identifier)
        .order_by(PolicyVersionRow.version.desc())
        .limit(1)
    ).first()
    version = (latest.version if latest else 0) + 1
    policy_version = PolicyVersionRow(
        id=new_id(),
        policy_id=row.identifier,
        version=version,
        config=config,
        checksum=checksum,
        created_at=now_utc(),
        created_by=origin,
        is_immutable=True,
    )
    session.add(policy_version)
    session.flush()
    row.current_version_id = policy_version.id
    audit(
        session,
        None,
        "policy_version_created",
        {"policy_id": row.identifier, "version": version, "checksum": checksum},
        policy_version_id=policy_version.id,
        origin=origin,
    )
    return policy_version


def seed_policies(session: Session) -> None:
    if session.get(TenantRow, DEFAULT_TENANT_ID) is None:
        stamp = now_utc()
        session.add(TenantRow(
            id=DEFAULT_TENANT_ID,
            slug=DEFAULT_TENANT_SLUG,
            name="Alex",
            status="ACTIVE",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.flush()
    for policy in DEMO_POLICIES:
        config = policy_to_config(policy)
        row = session.get(PolicyRow, policy.identifier)
        if row is None:
            row = PolicyRow(**config, tenant_id=DEFAULT_TENANT_ID, is_active=True)
            session.add(row)
            session.flush()
        if not row.current_version_id:
            for key, value in config.items():
                setattr(row, key, value)
        ensure_policy_version(session, row, config, "seed")
    audit(session, None, "policies_seeded", {"count": len(DEMO_POLICIES)}, origin="seed")


def list_policies(session: Session, tenant_id: str = DEFAULT_TENANT_ID) -> list[Policy]:
    rows = session.scalars(select(PolicyRow).where(
        PolicyRow.tenant_id == tenant_id,
        PolicyRow.is_active.is_(True),
    )).all()
    return [get_policy(session, row.identifier, tenant_id) for row in rows]


def get_policy(session: Session, identifier: str, tenant_id: str = DEFAULT_TENANT_ID) -> Policy:
    row = session.get(PolicyRow, identifier)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError(identifier)
    if row.current_version_id:
        return policy_from_version(session.get(PolicyVersionRow, row.current_version_id))
    return row_to_policy(row)


def get_active_policy_version(
    session: Session,
    identifier: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> PolicyVersionRow:
    row = session.get(PolicyRow, identifier)
    if row is None or row.tenant_id != tenant_id or not row.current_version_id:
        raise KeyError(identifier)
    return session.get(PolicyVersionRow, row.current_version_id)


def list_policy_versions(
    session: Session,
    identifier: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    get_policy(session, identifier, tenant_id)
    rows = session.scalars(
        select(PolicyVersionRow).where(PolicyVersionRow.policy_id == identifier).order_by(PolicyVersionRow.version)
    ).all()
    return [
        {
            "id": row.id,
            "policy_id": row.policy_id,
            "version": row.version,
            "config": row.config,
            "checksum": row.checksum,
            "created_at": row.created_at.isoformat(),
            "created_by": row.created_by,
            "is_immutable": row.is_immutable,
        }
        for row in rows
    ]


def update_policy(
    session: Session,
    identifier: str,
    changes: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Policy:
    row = session.get(PolicyRow, identifier)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError(identifier)
    config = policy_to_config(get_policy(session, identifier, tenant_id))
    for key in ["initial_wait_seconds", "tone", "escalation_steps"]:
        if key in changes:
            config[key] = changes[key]
    if "escalation_steps" in changes:
        config["allowed_actions"] = sorted(set(config["allowed_actions"]) | set(changes["escalation_steps"]))
    policy_version = ensure_policy_version(session, row, config, "api")
    for key, value in config.items():
        setattr(row, key, value)
    row.current_version_id = policy_version.id
    audit(
        session,
        None,
        "policy_updated",
        {"policy_id": identifier, "changes": changes},
        policy_version_id=policy_version.id,
        tenant_id=tenant_id,
    )
    return policy_from_version(policy_version)


def provision_morgan_owner_reply_grace_policy(
    session: Session,
    tenant_id: str = DEFAULT_TENANT_ID,
    *,
    origin: str = "owner_reply_grace_canary_provisioning",
) -> PolicyVersionRow:
    """Create, never rewrite, the exact immutable Morgan grace version."""
    identifier = "morgan_presence_autonomy_v1"
    row = session.get(PolicyRow, identifier)
    if row is None or row.tenant_id != tenant_id or not row.current_version_id:
        raise KeyError(identifier)
    current = session.get(PolicyVersionRow, row.current_version_id)
    if current is None:
        raise KeyError(row.current_version_id)
    config = dict(current.config)
    config.pop("owner_reply_grace_seconds", None)
    config["owner_reply_grace_allowed"] = True
    config["owner_reply_grace_default_seconds"] = 30
    config["owner_reply_grace_min_seconds"] = 0
    config["owner_reply_grace_max_seconds"] = 300
    config["owner_reply_grace_mode"] = "TRAILING_EDGE"
    version = ensure_policy_version(session, row, config, origin)
    row.current_version_id = version.id
    audit(
        session,
        None,
        "owner_reply_grace.policy_version_provisioned",
        {
            "policy_id": identifier,
            "policy_version_id": version.id,
            "owner_reply_grace_allowed": True,
            "owner_reply_grace_default_seconds": 30,
            "owner_reply_grace_min_seconds": 0,
            "owner_reply_grace_max_seconds": 300,
            "owner_reply_grace_mode": "TRAILING_EDGE",
        },
        policy_version_id=version.id,
        origin=origin,
        tenant_id=tenant_id,
    )
    session.flush()
    return version


def create_policy(
    session: Session,
    identifier: str,
    config: dict[str, Any],
    origin: str = "api",
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Policy:
    if session.get(PolicyRow, identifier):
        raise ValueError("policy already exists")
    row = PolicyRow(**config, tenant_id=tenant_id, is_active=True)
    session.add(row)
    session.flush()
    policy_version = ensure_policy_version(session, row, config, origin)
    audit(
        session,
        None,
        "policy_created",
        {"policy_id": identifier},
        policy_version_id=policy_version.id,
        origin=origin,
        tenant_id=tenant_id,
    )
    return policy_from_version(policy_version)


def activate_policy_version(
    session: Session,
    identifier: str,
    version_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Policy:
    row = session.get(PolicyRow, identifier)
    policy_version = session.get(PolicyVersionRow, version_id)
    if (
        row is None
        or row.tenant_id != tenant_id
        or policy_version is None
        or policy_version.policy_id != identifier
    ):
        raise KeyError(identifier)
    for key, value in policy_version.config.items():
        setattr(row, key, value)
    row.current_version_id = policy_version.id
    row.is_active = True
    audit(
        session,
        None,
        "policy_version_activated",
        {"policy_id": identifier},
        policy_version_id=policy_version.id,
        tenant_id=tenant_id,
    )
    return policy_from_version(policy_version)


def deactivate_policy(
    session: Session,
    identifier: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    row = session.get(PolicyRow, identifier)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError(identifier)
    row.is_active = False
    audit(
        session,
        None,
        "policy_deactivated",
        {"policy_id": identifier},
        policy_version_id=row.current_version_id,
        tenant_id=tenant_id,
    )


def mask_identifier(value: str, visible: int = 4) -> str:
    if len(value) <= visible:
        return "***"
    return f"***{value[-visible:]}"


def actor_binding_to_dict(row: ActorBindingRow, reveal_external: bool = False) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "external_actor_id": row.external_actor_id if reveal_external else mask_identifier(row.external_actor_id),
        "actor_key": row.actor_key,
        "display_name": row.display_name,
        "actor_category": row.actor_category,
        "active_context": row.active_context,
        "is_active": row.is_active,
        "metadata": row.binding_metadata,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_actor_bindings(
    session: Session,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[ActorBindingRow]:
    return session.scalars(
        select(ActorBindingRow)
        .where(ActorBindingRow.tenant_id == tenant_id)
        .order_by(ActorBindingRow.source, ActorBindingRow.actor_key)
    ).all()


def get_actor_binding(
    session: Session,
    binding_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow:
    row = session.get(ActorBindingRow, binding_id)
    if row is None or row.tenant_id != tenant_id:
        raise KeyError(binding_id)
    return row


def resolve_actor_binding(
    session: Session,
    source: str,
    external_actor_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow | None:
    return session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.source == source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
        )
    ).first()


def find_active_actor_binding_by_key(
    session: Session,
    actor_key: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow | None:
    return session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.actor_key == actor_key,
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
        )
    ).first()


def find_active_actor_binding_by_external_actor_id(
    session: Session,
    external_actor_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow | None:
    return session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
        )
    ).first()


def upsert_actor_binding(
    session: Session,
    source: str,
    external_actor_id: str,
    actor_key: str,
    actor_category: str,
    display_name: str | None = None,
    active_context: str | None = None,
    metadata: dict[str, Any] | None = None,
    is_active: bool = True,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow:
    stamp = now_utc()
    row = session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.source == source,
            ActorBindingRow.external_actor_id == external_actor_id,
            ActorBindingRow.tenant_id == tenant_id,
        )
    ).first()
    if row is None:
        row = ActorBindingRow(
            id=new_id(),
            tenant_id=tenant_id,
            source=source,
            external_actor_id=external_actor_id,
            actor_key=actor_key,
            display_name=display_name,
            actor_category=actor_category,
            active_context=active_context,
            is_active=is_active,
            binding_metadata=metadata or {},
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        audit(
            session,
            None,
            "actor_binding_created",
            {"source": source, "external_actor_id": mask_identifier(external_actor_id), "actor_key": actor_key},
            origin="admin",
            tenant_id=tenant_id,
        )
    else:
        previous = {
            "actor_key": row.actor_key,
            "actor_category": row.actor_category,
            "is_active": row.is_active,
        }
        row.actor_key = actor_key
        row.display_name = display_name
        row.actor_category = actor_category
        row.active_context = active_context
        row.is_active = is_active
        row.binding_metadata = metadata or {}
        row.updated_at = stamp
        audit(
            session,
            None,
            "actor_binding_updated",
            {
                "source": source,
                "external_actor_id": mask_identifier(external_actor_id),
                "actor_key": actor_key,
                "previous": previous,
            },
            origin="admin",
            tenant_id=tenant_id,
        )
    session.flush()
    return row


def set_actor_binding_active(
    session: Session,
    binding_id: str,
    active: bool,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> ActorBindingRow:
    row = get_actor_binding(session, binding_id, tenant_id)
    row.is_active = active
    row.updated_at = now_utc()
    audit(
        session,
        None,
        "actor_binding_activation_changed",
        {"binding_id": binding_id, "source": row.source, "active": active},
        origin="admin",
        tenant_id=tenant_id,
    )
    session.flush()
    return row


def interaction_to_dict(session: Session, row: InteractionRow) -> dict[str, Any]:
    decisions = session.scalars(
        select(DecisionRow).where(DecisionRow.interaction_id == row.id).order_by(DecisionRow.created_at)
    ).all()
    actions = session.scalars(
        select(ActionAttemptRow)
        .where(ActionAttemptRow.interaction_id == row.id)
        .order_by(ActionAttemptRow.step_index, ActionAttemptRow.created_at)
    ).all()
    timers = session.scalars(
        select(TimerRow).where(TimerRow.interaction_id == row.id).order_by(TimerRow.due_at)
    ).all()
    outbox = session.scalars(
        select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == row.id).order_by(OutboxMessageRow.created_at)
    ).all()
    audit_rows = session.scalars(
        select(AuditEventRow).where(AuditEventRow.interaction_id == row.id).order_by(AuditEventRow.created_at)
    ).all()
    return {
        "id": row.id,
        "state": row.state,
        "event_type": row.event_type,
        "contact": {
            "synthetic_id": row.contact_id,
            "display_name": row.contact_name,
            "relationship_category": row.relationship_category,
        },
        "active_context": row.active_context,
        "inbound_text": row.inbound_text,
        "policy_id": row.policy_id,
        "policy_version_id": row.policy_version_id,
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "lia_speech": row.lia_speech,
        "decisions": [
            {
                "policy_id": d.policy_id,
                "policy_version_id": d.policy_version_id,
                "correlation_id": d.correlation_id,
                "matched_rules": d.matched_rules,
                "reason": d.reason,
                "created_at": d.created_at.isoformat(),
            }
            for d in decisions
        ],
        "actions": [
            {
                "id": a.id,
                "action_key": a.action_key,
                "step_index": a.step_index,
                "state": a.state,
                "outbox_message_id": a.outbox_message_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ],
        "timers": [
            {
                "id": t.id,
                "kind": t.kind,
                "action_attempt_id": t.action_attempt_id,
                "due_at": t.due_at.isoformat(),
                "status": t.status,
                "claimed_by": t.claimed_by,
                "attempt_count": t.attempt_count,
            }
            for t in timers
        ],
        "outbox": [
            {
                "id": item.id,
                "destination": item.destination,
                "status": item.status,
                "idempotency_key": item.idempotency_key,
                "attempt_count": item.attempt_count,
            }
            for item in outbox
        ],
        "audit_events": [
            {
                "event_type": a.event_type,
                "payload": a.payload,
                "created_at": a.created_at.isoformat(),
                "correlation_id": a.correlation_id,
                "causation_id": a.causation_id,
                "previous_state": a.previous_state,
                "next_state": a.next_state,
                "policy_version_id": a.policy_version_id,
            }
            for a in audit_rows
        ],
    }


def create_interaction_row(
    session: Session,
    event_type: str,
    contact: ContactIdentity,
    active_context: str | None,
    inbound_text: str,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> InteractionRow:
    stamp = now_utc()
    row = InteractionRow(
        id=new_id(),
        tenant_id=tenant_id,
        event_type=event_type,
        contact_id=contact.synthetic_id,
        contact_name=contact.display_name,
        relationship_category=contact.relationship_category,
        active_context=active_context,
        inbound_text=inbound_text,
        state=InteractionState.RECEIVED.value,
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        row.id,
        "interaction_created",
        {"event_type": event_type},
        correlation_id=correlation_id,
        causation_id=causation_id,
        next_state=InteractionState.RECEIVED.value,
        tenant_id=tenant_id,
    )
    return row


def to_domain_interaction(row: InteractionRow) -> Interaction:
    return Interaction(
        id=row.id,
        event_type=row.event_type,
        contact=ContactIdentity(row.contact_id, row.contact_name, row.relationship_category),
        active_context=row.active_context,
        inbound_text=row.inbound_text,
        state=InteractionState(row.state),
        policy_id=row.policy_id,
        lia_speech=row.lia_speech,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def set_state(row: InteractionRow, state: InteractionState) -> tuple[str, str]:
    previous = row.state
    row.state = state.value
    row.updated_at = now_utc()
    return previous, row.state


def add_action(
    session: Session,
    interaction_id: str,
    action_key: str,
    step_index: int,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> ActionAttemptRow:
    stamp = now_utc()
    outbox = OutboxMessageRow(
        id=new_id(),
        interaction_id=interaction_id,
        action_type="dispatch_action",
        destination=action_key,
        payload={"action_key": action_key, "step_index": step_index},
        status="PENDING",
        created_at=stamp,
        available_at=stamp,
        idempotency_key=f"{interaction_id}:{step_index}:{action_key}",
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    session.add(outbox)
    session.flush()
    action = ActionAttemptRow(
        id=new_id(),
        interaction_id=interaction_id,
        action_key=action_key,
        step_index=step_index,
        state=ActionState.DISPATCHED.value,
        outbox_message_id=outbox.id,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(action)
    audit(
        session,
        interaction_id,
        "outbox_created",
        {"outbox_id": outbox.id, "destination": action_key, "idempotency_key": outbox.idempotency_key},
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    audit(
        session,
        interaction_id,
        "action_dispatched",
        {"action_key": action_key, "step_index": step_index},
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    return action


def add_ack_timer(
    session: Session,
    interaction_id: str,
    action_id: str,
    seconds: int,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> TimerRow:
    timer = TimerRow(
        id=new_id(),
        interaction_id=interaction_id,
        action_attempt_id=action_id,
        kind=TimerKind.ACTION_ACK_TIMEOUT.value,
        due_at=now_utc() + timedelta(seconds=seconds),
        status="PENDING",
        correlation_id=correlation_id,
        causation_id=causation_id,
        created_at=now_utc(),
    )
    session.add(timer)
    session.add(
        QueueRow(
            id=timer.id,
            kind="timer",
            payload={"timer_id": timer.id, "interaction_id": interaction_id},
            status="PENDING",
            created_at=now_utc(),
        )
    )
    audit(
        session,
        interaction_id,
        "timer_created",
        {"timer_id": timer.id, "seconds": seconds},
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    return timer


def create_inbound_event(
    session: Session,
    source: str,
    external_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    received_at: datetime | None = None,
) -> InboundEventRow:
    lineage_classification = str(payload.get("lineage_classification") or "HISTORICAL_UNKNOWN")
    if lineage_classification not in {"ORGANIC", "SYNTHETIC", "HISTORICAL_UNKNOWN"}:
        raise ValueError("INBOUND_LINEAGE_CLASSIFICATION_INVALID")
    scenario_run_id = payload.get("scenario_run_id")
    scenario_step_run_id = payload.get("scenario_step_run_id")
    if lineage_classification == "SYNTHETIC":
        if not payload.get("scenario_id") or not scenario_run_id or not payload.get("stimulus_id"):
            raise ValueError("SYNTHETIC_SCENARIO_LINEAGE_REQUIRED")
    elif scenario_run_id or scenario_step_run_id or payload.get("scenario_id"):
        raise ValueError("NON_SYNTHETIC_SCENARIO_LINEAGE_FORBIDDEN")
    row = InboundEventRow(
        id=new_id(),
        tenant_id=tenant_id,
        source=source,
        external_event_id=external_event_id,
        event_type=event_type,
        payload=payload,
        payload_hash=stable_hash(payload),
        received_at=received_at or now_utc(),
        status="RECEIVED",
        correlation_id=correlation_id,
        lineage_classification=lineage_classification,
        scenario_run_id=scenario_run_id,
        scenario_step_run_id=scenario_step_run_id,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        None,
        "inbound_event_received",
        {"source": source, "external_event_id": external_event_id},
        correlation_id,
        row.id,
        tenant_id=tenant_id,
    )
    return row
