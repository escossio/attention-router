"""Persistent, policy-independent owner deny gate. No transport identity discovery."""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from attention_router.application.owner_operational_control import (
    OperationalControlConflict,
    OperationalControlError,
)
from attention_router.application.platform.context import resolve_represented_subject
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    OwnerAutomationControlChangeRow,
    OwnerAutomationControlRow,
)
from attention_router.infrastructure.repository import audit


OWNER_AUTOMATION_PAUSED = "OWNER_AUTOMATION_PAUSED"


@dataclass(frozen=True)
class OwnerAutomationControlMutation:
    control: OwnerAutomationControlRow
    changed: bool
    duplicate: bool


def lock_automation_scope(session: Session, tenant_id: str, owner: str, *, shared=False) -> None:
    """Serialize commands with the last-mile send, including an absent override.

    Dispatch holds only this shared scope lock across its bounded network call;
    commands take the exclusive lock. A committed pause therefore precedes a
    denied send, or follows an already-authorized in-flight send, never races it.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        value = int(stable_hash(f"owner-automation:{tenant_id}:{owner}")[:16], 16)
        if value >= 2**63:
            value -= 2**64
        function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        session.execute(text(f"SELECT {function}(:key)"), {"key": value})


def automation_denial_reason(session: Session, tenant_id: str, *, lock=False) -> str | None:
    represented = resolve_represented_subject(session, tenant_id)
    if represented is None:
        # Legacy tenants without a configured control retain their old gates.
        # Losing/ambiguating an owner must never bypass a persisted override.
        exists = session.scalar(
            select(OwnerAutomationControlRow.id)
            .where(OwnerAutomationControlRow.tenant_id == tenant_id)
            .limit(1)
        )
        return "OWNER_AUTOMATION_OWNER_UNRESOLVED" if exists else None
    if lock:
        lock_automation_scope(session, tenant_id, represented.entity_id, shared=True)
    # Scalar SQL deliberately bypasses Session identity-map cached row values.
    enabled = session.scalar(
        select(OwnerAutomationControlRow.automatic_responses_enabled).where(
            OwnerAutomationControlRow.tenant_id == tenant_id,
            OwnerAutomationControlRow.represented_owner_actor_key == represented.entity_id,
        )
    )
    return OWNER_AUTOMATION_PAUSED if enabled is False else None


def set_automatic_responses_enabled(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    enabled: bool,
    authority: OperatorAuthority,
    source_channel: str,
    source_event_id: str,
    provenance: dict,
) -> OwnerAutomationControlMutation:
    if not isinstance(enabled, bool):
        raise OperationalControlError("AUTOMATIC_RESPONSES_ENABLED_MUST_BE_BOOLEAN")
    if not source_channel.strip() or not source_event_id.strip():
        raise OperationalControlError("CONTROL_SOURCE_EVENT_REQUIRED")
    represented = resolve_represented_subject(session, tenant_id)
    if (
        authority.tenant_id != tenant_id
        or not authority.can_issue_owner_commands
        or represented is None
        or represented.entity_id != represented_owner_actor_key
        or authority.operator_actor_id != represented.entity_id
    ):
        raise OwnerCommandUnauthorized("OWNER_COMMAND_REQUIRES_REPRESENTED_OWNER")
    lock_automation_scope(session, tenant_id, represented_owner_actor_key)
    duplicate = session.scalar(
        select(OwnerAutomationControlChangeRow).where(
            OwnerAutomationControlChangeRow.tenant_id == tenant_id,
            OwnerAutomationControlChangeRow.source_channel == source_channel,
            OwnerAutomationControlChangeRow.source_event_id == source_event_id,
        )
    )
    row = session.scalar(
        select(OwnerAutomationControlRow)
        .where(
            OwnerAutomationControlRow.tenant_id == tenant_id,
            OwnerAutomationControlRow.represented_owner_actor_key == represented_owner_actor_key,
        )
        .execution_options(populate_existing=True)
    )
    if duplicate is not None:
        if (
            duplicate.represented_owner_actor_key != represented_owner_actor_key
            or duplicate.requested_enabled != enabled
            or row is None
        ):
            raise OperationalControlConflict("CONTROL_COMMAND_IDEMPOTENCY_CONFLICT")
        return OwnerAutomationControlMutation(row, False, True)
    previous_revision = row.revision if row else 0
    previous_enabled = row.automatic_responses_enabled if row else True
    changed = previous_enabled != enabled
    stamp = now_utc()
    if row is None:
        row = OwnerAutomationControlRow(
            id=new_id(),
            tenant_id=tenant_id,
            represented_owner_actor_key=represented_owner_actor_key,
            automatic_responses_enabled=enabled,
            revision=int(changed),
            updated_by_actor_key=authority.operator_actor_id,
            authorization_source="OWNER_COMMAND",
            source_channel=source_channel,
            source_event_id=source_event_id,
            provenance=dict(provenance),
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
    elif changed:
        row.automatic_responses_enabled = enabled
        row.revision += 1
        row.updated_by_actor_key = authority.operator_actor_id
        row.authorization_source = "OWNER_COMMAND"
        row.source_channel = source_channel
        row.source_event_id = source_event_id
        row.provenance = dict(provenance)
        row.updated_at = stamp
    session.flush()
    session.add(
        OwnerAutomationControlChangeRow(
            id=new_id(),
            tenant_id=tenant_id,
            control_id=row.id,
            represented_owner_actor_key=represented_owner_actor_key,
            source_channel=source_channel,
            source_event_id=source_event_id,
            requested_enabled=enabled,
            previous_revision=previous_revision,
            resulting_revision=row.revision,
            changed=changed,
            provenance=dict(provenance),
            created_at=stamp,
        )
    )
    audit(
        session,
        None,
        "owner_automation_control.changed" if changed else "owner_automation_control.unchanged",
        {
            "control_id": row.id,
            "enabled": enabled,
            "revision": row.revision,
            "source_channel": source_channel,
            "source_event_hash": stable_hash(source_event_id),
        },
        tenant_id=tenant_id,
        origin="owner_control",
        # Causation is the canonical receipt UUID, not a provider event ID
        # (which can exceed 64 chars and embed transport identity).
        causation_id=provenance.get("receipt_id"),
    )
    session.flush()
    return OwnerAutomationControlMutation(row, changed, False)
