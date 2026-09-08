from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from attention_router.application.platform.context import resolve_represented_subject
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ConversationResponseGraceWindowRow,
    OwnerOperationalControlChangeRow,
    OwnerOperationalControlRow,
    PolicyRow,
    PolicyVersionRow,
)
from attention_router.infrastructure.repository import audit


OWNER_REPLY_GRACE_CONTROL_KEY = "owner_reply_grace"
OWNER_REPLY_GRACE_MODE = "TRAILING_EDGE"
GLOBAL_GRACE_CONTRACT_VERSION = "v1"
GLOBAL_GRACE_DEFAULT_SECONDS = 30
GLOBAL_GRACE_MIN_SECONDS = 0
GLOBAL_GRACE_MAX_SECONDS = 300
CONTROL_SOURCE_POLICY_DEFAULT = "POLICY_DEFAULT"
CONTROL_SOURCE_GLOBAL_DEFAULT = "GLOBAL_DEFAULT"
CONTROL_SOURCE_OWNER_OVERRIDE = "OWNER_OVERRIDE"
AUTHORIZED_CONTROL_SOURCES = {
    "OWNER_COMMAND",
    "CONTROL_PANEL",
    "ADMIN_API",
    "ADMIN_CLI",
    "TEST",
}


class OperationalControlError(ValueError):
    pass


class OperationalControlUnauthorized(PermissionError):
    pass


class OperationalControlConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerReplyGracePolicyAuthority:
    allowed: bool
    default_seconds: int = 0
    min_seconds: int = 0
    max_seconds: int = 0
    mode: str | None = None
    reason_code: str = "POLICY_GRACE_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class EffectiveOwnerReplyGraceControl:
    enabled: bool
    effective_seconds: int | None
    source: str
    control_id: str | None
    control_revision: int | None
    policy_version_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class OwnerReplyGraceControlMutation:
    control: EffectiveOwnerReplyGraceControl
    changed: bool
    duplicate: bool
    affected_open_windows: int


class OwnerReplyGraceCommandType(StrEnum):
    SET_OWNER_REPLY_GRACE_SECONDS = "SET_OWNER_REPLY_GRACE_SECONDS"
    SET_OWNER_REPLY_GRACE_ENABLED = "SET_OWNER_REPLY_GRACE_ENABLED"


@dataclass(frozen=True, slots=True)
class OwnerReplyGraceControlCommand:
    command_id: str
    command_type: OwnerReplyGraceCommandType
    value: int | bool
    policy_id: str | None
    source_channel: str

    def __post_init__(self) -> None:
        if not self.command_id.strip():
            raise OperationalControlError("CONTROL_COMMAND_ID_REQUIRED")
        if self.policy_id is not None and not self.policy_id.strip():
            raise OperationalControlError("CONTROL_POLICY_ID_REQUIRED")
        if not self.source_channel.strip():
            raise OperationalControlError("CONTROL_SOURCE_CHANNEL_REQUIRED")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _strict_int(config: dict[str, Any], key: str) -> int | None:
    value = config.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def owner_reply_grace_policy_authority(
    policy_version: PolicyVersionRow | None,
) -> OwnerReplyGracePolicyAuthority:
    if policy_version is None:
        return OwnerReplyGracePolicyAuthority(False, reason_code="POLICY_VERSION_MISSING")
    config = policy_version.config or {}
    if config.get("owner_reply_grace_allowed") is not True:
        return OwnerReplyGracePolicyAuthority(False, reason_code="POLICY_GRACE_NOT_ALLOWED")
    default_seconds = _strict_int(config, "owner_reply_grace_default_seconds")
    min_seconds = _strict_int(config, "owner_reply_grace_min_seconds")
    max_seconds = _strict_int(config, "owner_reply_grace_max_seconds")
    mode = config.get("owner_reply_grace_mode")
    if (
        default_seconds is None
        or min_seconds is None
        or max_seconds is None
        or min_seconds < 0
        or not min_seconds <= default_seconds <= max_seconds
        or mode != OWNER_REPLY_GRACE_MODE
    ):
        return OwnerReplyGracePolicyAuthority(False, reason_code="POLICY_GRACE_CONTRACT_INVALID")
    return OwnerReplyGracePolicyAuthority(
        True,
        default_seconds=default_seconds,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        mode=mode,
        reason_code="POLICY_GRACE_ALLOWED",
    )


def lock_owner_control_scope(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
) -> None:
    """Serialize control changes with inbound reads on PostgreSQL."""
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    digest = stable_hash(
        f"owner-control:{tenant_id}:{represented_owner_actor_key}:"
        f"{OWNER_REPLY_GRACE_CONTROL_KEY}:{policy_id}"
    )
    lock_key = int(digest[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _control_query(
    *, tenant_id: str, represented_owner_actor_key: str, policy_id: str | None
):
    return select(OwnerOperationalControlRow).where(
        OwnerOperationalControlRow.tenant_id == tenant_id,
        OwnerOperationalControlRow.represented_owner_actor_key == represented_owner_actor_key,
        OwnerOperationalControlRow.control_key == OWNER_REPLY_GRACE_CONTROL_KEY,
        OwnerOperationalControlRow.policy_id == policy_id,
        OwnerOperationalControlRow.scope_type == ("GLOBAL" if policy_id is None else "POLICY"),
    )


def global_grace_authority() -> OwnerReplyGracePolicyAuthority:
    return OwnerReplyGracePolicyAuthority(True, GLOBAL_GRACE_DEFAULT_SECONDS,
        GLOBAL_GRACE_MIN_SECONDS, GLOBAL_GRACE_MAX_SECONDS, OWNER_REPLY_GRACE_MODE,
        "GLOBAL_GRACE_CONTRACT")


def resolve_global_owner_reply_grace_control(
    session: Session, *, tenant_id: str, represented_owner_actor_key: str | None,
) -> EffectiveOwnerReplyGraceControl:
    represented = resolve_represented_subject(session, tenant_id)
    if not represented or represented.entity_id != represented_owner_actor_key:
        return EffectiveOwnerReplyGraceControl(False, None, CONTROL_SOURCE_GLOBAL_DEFAULT, None,
            None, None, "REPRESENTED_OWNER_UNRESOLVED")
    row = session.scalar(_control_query(tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key, policy_id=None)
        .execution_options(populate_existing=True))
    if row is None:
        return EffectiveOwnerReplyGraceControl(True, GLOBAL_GRACE_DEFAULT_SECONDS,
            CONTROL_SOURCE_GLOBAL_DEFAULT, None, None, None, "GLOBAL_DEFAULT_APPLIED")
    valid = GLOBAL_GRACE_MIN_SECONDS <= row.integer_value <= GLOBAL_GRACE_MAX_SECONDS
    return EffectiveOwnerReplyGraceControl(row.enabled and valid, row.integer_value,
        "OWNER_OVERRIDE", row.id, row.revision, None,
        "OWNER_OVERRIDE_APPLIED" if valid else "OWNER_OVERRIDE_OUT_OF_GLOBAL_BOUNDS")


def resolve_owner_reply_grace_control(
    session: Session,
    *,
    policy_version: PolicyVersionRow | None,
    represented_owner_actor_key: str | None,
) -> EffectiveOwnerReplyGraceControl:
    authority = owner_reply_grace_policy_authority(policy_version)
    if not authority.allowed:
        return EffectiveOwnerReplyGraceControl(
            False,
            None,
            CONTROL_SOURCE_POLICY_DEFAULT,
            None,
            None,
            policy_version.id if policy_version else None,
            authority.reason_code,
        )
    if not represented_owner_actor_key:
        return EffectiveOwnerReplyGraceControl(
            False,
            authority.default_seconds,
            CONTROL_SOURCE_POLICY_DEFAULT,
            None,
            None,
            policy_version.id,
            "REPRESENTED_OWNER_UNRESOLVED",
        )
    row = session.scalar(
        _control_query(
            tenant_id=_policy_tenant_id(session, policy_version),
            represented_owner_actor_key=represented_owner_actor_key,
            policy_id=policy_version.policy_id,
        )
    )
    if row is None:
        return EffectiveOwnerReplyGraceControl(
            True,
            authority.default_seconds,
            CONTROL_SOURCE_POLICY_DEFAULT,
            None,
            None,
            policy_version.id,
            "POLICY_DEFAULT_APPLIED",
        )
    if not authority.min_seconds <= row.integer_value <= authority.max_seconds:
        return EffectiveOwnerReplyGraceControl(
            False,
            authority.default_seconds,
            CONTROL_SOURCE_OWNER_OVERRIDE,
            row.id,
            row.revision,
            policy_version.id,
            "OWNER_OVERRIDE_OUT_OF_POLICY_BOUNDS",
        )
    return EffectiveOwnerReplyGraceControl(
        row.enabled,
        row.integer_value,
        CONTROL_SOURCE_OWNER_OVERRIDE,
        row.id,
        row.revision,
        policy_version.id,
        "OWNER_OVERRIDE_APPLIED" if row.enabled else "OWNER_OVERRIDE_DISABLED",
    )


def _policy_tenant_id(session: Session, policy_version: PolicyVersionRow) -> str:
    row = session.get(PolicyRow, policy_version.policy_id)
    if row is None:
        raise OperationalControlError("POLICY_NOT_FOUND")
    return row.tenant_id


def _active_policy_version(
    session: Session, *, tenant_id: str, policy_id: str
) -> PolicyVersionRow:
    policy = session.get(PolicyRow, policy_id)
    if policy is None or policy.tenant_id != tenant_id or not policy.current_version_id:
        raise OperationalControlError("POLICY_NOT_FOUND")
    version = session.get(PolicyVersionRow, policy.current_version_id)
    if version is None:
        raise OperationalControlError("POLICY_VERSION_NOT_FOUND")
    authority = owner_reply_grace_policy_authority(version)
    if not authority.allowed:
        raise OperationalControlError(authority.reason_code)
    return version


def _authorize_control_change(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    updated_by_actor_key: str,
    authorization_source: str,
) -> None:
    if authorization_source not in AUTHORIZED_CONTROL_SOURCES:
        raise OperationalControlUnauthorized("CONTROL_AUTHORIZATION_SOURCE_INVALID")
    represented = resolve_represented_subject(session, tenant_id)
    if represented is None or represented.entity_id != represented_owner_actor_key:
        raise OperationalControlUnauthorized("REPRESENTED_OWNER_UNRESOLVED")
    if updated_by_actor_key != represented_owner_actor_key:
        raise OperationalControlUnauthorized("OWNER_CONTROL_REQUIRES_REPRESENTED_OWNER")


def get_owner_reply_grace_control(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
) -> EffectiveOwnerReplyGraceControl:
    represented = resolve_represented_subject(session, tenant_id)
    if represented is None or represented.entity_id != represented_owner_actor_key:
        raise OperationalControlUnauthorized("REPRESENTED_OWNER_UNRESOLVED")
    if policy_id is None:
        return resolve_global_owner_reply_grace_control(session, tenant_id=tenant_id,
            represented_owner_actor_key=represented_owner_actor_key)
    policy_version = _active_policy_version(
        session, tenant_id=tenant_id, policy_id=policy_id
    )
    return resolve_owner_reply_grace_control(
        session,
        policy_version=policy_version,
        represented_owner_actor_key=represented_owner_actor_key,
    )


def _matching_change(
    session: Session,
    *,
    tenant_id: str,
    source_channel: str,
    source_event_id: str | None,
) -> OwnerOperationalControlChangeRow | None:
    if not source_event_id:
        return None
    return session.scalar(
        select(OwnerOperationalControlChangeRow).where(
            OwnerOperationalControlChangeRow.tenant_id == tenant_id,
            OwnerOperationalControlChangeRow.source_channel == source_channel,
            OwnerOperationalControlChangeRow.source_event_id == source_event_id,
        )
    )


def _assert_duplicate_matches(
    change: OwnerOperationalControlChangeRow,
    *,
    represented_owner_actor_key: str,
    policy_id: str | None,
    requested_enabled: bool | None,
    requested_integer_value: int | None,
) -> None:
    if (
        change.represented_owner_actor_key != represented_owner_actor_key
        or change.policy_id != policy_id
        or change.control_key != OWNER_REPLY_GRACE_CONTROL_KEY
        or change.requested_enabled != requested_enabled
        or change.requested_integer_value != requested_integer_value
    ):
        raise OperationalControlConflict("CONTROL_COMMAND_PAYLOAD_CONFLICT")


def _recalculate_open_windows(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
    control: OwnerOperationalControlRow,
    timestamp: datetime,
) -> int:
    policy_versions = select(PolicyVersionRow.id).where(PolicyVersionRow.policy_id == policy_id)
    query = (
        select(ConversationResponseGraceWindowRow)
        .where(
            ConversationResponseGraceWindowRow.tenant_id == tenant_id,
            ConversationResponseGraceWindowRow.represented_owner_actor_key
            == represented_owner_actor_key,
            (ConversationResponseGraceWindowRow.policy_version_id.in_(policy_versions)
             if policy_id is not None else True),
            ConversationResponseGraceWindowRow.state == "OPEN",
        )
        .order_by(ConversationResponseGraceWindowRow.id)
    )
    if policy_id is not None:
        # Legacy policy controls cannot mutate windows governed by the global contract.
        query = query.where(ConversationResponseGraceWindowRow.operational_control_id == control.id)
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    windows = session.scalars(query).all()
    for window in windows:
        previous_due_at = _utc(window.due_at)
        previous_enabled = window.auto_release_enabled
        previous_seconds = window.effective_grace_seconds
        window.operational_control_id = control.id
        window.operational_control_revision = control.revision
        window.operational_control_source = CONTROL_SOURCE_OWNER_OVERRIDE
        window.auto_release_enabled = control.enabled
        window.effective_grace_seconds = control.integer_value
        window.due_at = _utc(window.last_inbound_at) + timedelta(seconds=control.integer_value)
        window.claimed_at = None
        window.claimed_by = None
        window.claimed_generation = None
        window.updated_at = timestamp
        audit(
            session,
            window.anchor_interaction_id,
            "grace.deadline_recalculated",
            {
                "grace_window_id": window.id,
                "control_revision": control.revision,
                "previous_enabled": previous_enabled,
                "new_enabled": control.enabled,
                "previous_seconds": previous_seconds,
                "new_seconds": control.integer_value,
                "previous_due_at": previous_due_at.isoformat(),
                "new_due_at": window.due_at.isoformat(),
            },
            causation_id=control.source_event_id,
            policy_version_id=window.policy_version_id,
            origin="owner_operational_control",
            tenant_id=tenant_id,
            created_at=timestamp,
        )
    return len(windows)


def _set_owner_reply_grace_control(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
    updated_by_actor_key: str,
    authorization_source: str,
    source_channel: str,
    source_event_id: str | None,
    requested_enabled: bool | None,
    requested_integer_value: int | None,
    expected_revision: int | None,
    provenance: dict[str, Any] | None,
    timestamp: datetime | None,
) -> OwnerReplyGraceControlMutation:
    if not source_channel.strip():
        raise OperationalControlError("CONTROL_SOURCE_CHANNEL_REQUIRED")
    if source_event_id is not None and not source_event_id.strip():
        raise OperationalControlError("CONTROL_SOURCE_EVENT_ID_INVALID")
    _authorize_control_change(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        updated_by_actor_key=updated_by_actor_key,
        authorization_source=authorization_source,
    )
    policy_version = (_active_policy_version(session, tenant_id=tenant_id, policy_id=policy_id)
                      if policy_id is not None else None)
    authority = owner_reply_grace_policy_authority(policy_version) if policy_version else global_grace_authority()
    def effective_control():
        if policy_version:
            return resolve_owner_reply_grace_control(session, policy_version=policy_version,
                represented_owner_actor_key=represented_owner_actor_key)
        return resolve_global_owner_reply_grace_control(session, tenant_id=tenant_id,
                represented_owner_actor_key=represented_owner_actor_key)
    if requested_integer_value is not None and not (
        authority.min_seconds <= requested_integer_value <= authority.max_seconds
    ):
        raise OperationalControlError("GRACE_SECONDS_OUT_OF_POLICY_BOUNDS")
    stamp = _utc(timestamp or now_utc())
    lock_owner_control_scope(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        policy_id=policy_id,
    )
    duplicate = _matching_change(
        session,
        tenant_id=tenant_id,
        source_channel=source_channel,
        source_event_id=source_event_id,
    )
    if duplicate is not None:
        duplicate_policy_id = policy_id
        if policy_id is None and duplicate.policy_id is not None:
            migrated = session.get(OwnerOperationalControlRow, duplicate.control_id)
            if (migrated and migrated.scope_type == "GLOBAL"
                and migrated.tenant_id == tenant_id
                and migrated.represented_owner_actor_key == represented_owner_actor_key
                and (migrated.provenance.get("global_grace_migration") or {}).get("legacy_policy_id") == duplicate.policy_id):
                duplicate_policy_id = duplicate.policy_id
        _assert_duplicate_matches(
            duplicate,
            represented_owner_actor_key=represented_owner_actor_key,
            policy_id=duplicate_policy_id,
            requested_enabled=requested_enabled,
            requested_integer_value=requested_integer_value,
        )
        effective = effective_control()
        return OwnerReplyGraceControlMutation(effective, False, True, 0)

    query = _control_query(
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        policy_id=policy_id,
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    row = session.scalar(query)
    previous_revision = row.revision if row else 0
    if expected_revision is not None and expected_revision != previous_revision:
        raise OperationalControlConflict("CONTROL_REVISION_CONFLICT")
    previous_enabled = row.enabled if row else True
    previous_seconds = row.integer_value if row else authority.default_seconds
    new_enabled = previous_enabled if requested_enabled is None else requested_enabled
    new_seconds = previous_seconds if requested_integer_value is None else requested_integer_value
    changed = row is None or new_enabled != previous_enabled or new_seconds != previous_seconds
    if row is None:
        row = OwnerOperationalControlRow(
            id=new_id(),
            tenant_id=tenant_id,
            represented_owner_actor_key=represented_owner_actor_key,
            control_key=OWNER_REPLY_GRACE_CONTROL_KEY,
            policy_id=policy_id,
            enabled=new_enabled,
            scope_type="GLOBAL" if policy_id is None else "POLICY",
            integer_value=new_seconds,
            revision=1,
            updated_by_actor_key=updated_by_actor_key,
            authorization_source=authorization_source,
            source_channel=source_channel,
            source_event_id=source_event_id,
            provenance=dict(provenance or {}),
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        session.flush()
    elif changed:
        row.enabled = new_enabled
        row.integer_value = new_seconds
        row.revision += 1
        row.updated_by_actor_key = updated_by_actor_key
        row.authorization_source = authorization_source
        row.source_channel = source_channel
        row.source_event_id = source_event_id
        migration_provenance = row.provenance.get("global_grace_migration")
        row.provenance = dict(provenance or {})
        if migration_provenance is not None:
            row.provenance["global_grace_migration"] = migration_provenance
        row.updated_at = stamp
        session.flush()

    affected = (
        _recalculate_open_windows(
            session,
            tenant_id=tenant_id,
            represented_owner_actor_key=represented_owner_actor_key,
            policy_id=policy_id,
            control=row,
            timestamp=stamp,
        )
        if changed
        else 0
    )
    change = OwnerOperationalControlChangeRow(
        id=new_id(),
        tenant_id=tenant_id,
        control_id=row.id,
        represented_owner_actor_key=represented_owner_actor_key,
        control_key=OWNER_REPLY_GRACE_CONTROL_KEY,
        policy_id=policy_id,
        source_channel=source_channel,
        source_event_id=source_event_id,
        requested_enabled=requested_enabled,
        requested_integer_value=requested_integer_value,
        previous_revision=previous_revision,
        resulting_revision=row.revision,
        changed=changed,
        provenance=dict(provenance or {}),
        created_at=stamp,
    )
    session.add(change)
    if changed:
        audit(
            session,
            None,
            "operational_control.changed",
            {
                "control_key": OWNER_REPLY_GRACE_CONTROL_KEY,
                "control_id": row.id,
                "previous_enabled": previous_enabled,
                "new_enabled": new_enabled,
                "previous_seconds": previous_seconds,
                "new_seconds": new_seconds,
                "previous_revision": previous_revision,
                "new_revision": row.revision,
                "updated_by_actor_key": updated_by_actor_key,
                "authorization_source": authorization_source,
                "source_channel": source_channel,
                "source_event_id": source_event_id,
                "affected_open_windows": affected,
            },
            causation_id=source_event_id,
            policy_version_id=policy_version.id if policy_version else None,
            origin="owner_operational_control",
            tenant_id=tenant_id,
            created_at=stamp,
        )
        if previous_enabled != new_enabled:
            audit(
                session,
                None,
                "owner_reply_grace.enabled" if new_enabled else "owner_reply_grace.disabled",
                {
                    "control_id": row.id,
                    "previous_revision": previous_revision,
                    "new_revision": row.revision,
                    "affected_open_windows": affected,
                },
                causation_id=source_event_id,
                policy_version_id=policy_version.id if policy_version else None,
                origin="owner_operational_control",
                tenant_id=tenant_id,
                created_at=stamp,
            )
        if previous_seconds != new_seconds:
            audit(
                session,
                None,
                "owner_reply_grace.seconds_changed",
                {
                    "control_id": row.id,
                    "previous_seconds": previous_seconds,
                    "new_seconds": new_seconds,
                    "previous_revision": previous_revision,
                    "new_revision": row.revision,
                    "affected_open_windows": affected,
                },
                causation_id=source_event_id,
                policy_version_id=policy_version.id if policy_version else None,
                origin="owner_operational_control",
                tenant_id=tenant_id,
                created_at=stamp,
            )
    session.flush()
    effective = effective_control()
    return OwnerReplyGraceControlMutation(effective, changed, False, affected)


def set_owner_reply_grace_seconds(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
    grace_seconds: int,
    updated_by_actor_key: str,
    authorization_source: str,
    source_channel: str,
    source_event_id: str | None = None,
    expected_revision: int | None = None,
    provenance: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> OwnerReplyGraceControlMutation:
    if not isinstance(grace_seconds, int) or isinstance(grace_seconds, bool):
        raise OperationalControlError("GRACE_SECONDS_MUST_BE_INTEGER")
    return _set_owner_reply_grace_control(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        policy_id=policy_id,
        updated_by_actor_key=updated_by_actor_key,
        authorization_source=authorization_source,
        source_channel=source_channel,
        source_event_id=source_event_id,
        requested_enabled=None,
        requested_integer_value=grace_seconds,
        expected_revision=expected_revision,
        provenance=provenance,
        timestamp=timestamp,
    )


def set_owner_reply_grace_enabled(
    session: Session,
    *,
    tenant_id: str,
    represented_owner_actor_key: str,
    policy_id: str | None,
    enabled: bool,
    updated_by_actor_key: str,
    authorization_source: str,
    source_channel: str,
    source_event_id: str | None = None,
    expected_revision: int | None = None,
    provenance: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> OwnerReplyGraceControlMutation:
    if not isinstance(enabled, bool):
        raise OperationalControlError("GRACE_ENABLED_MUST_BE_BOOLEAN")
    return _set_owner_reply_grace_control(
        session,
        tenant_id=tenant_id,
        represented_owner_actor_key=represented_owner_actor_key,
        policy_id=policy_id,
        updated_by_actor_key=updated_by_actor_key,
        authorization_source=authorization_source,
        source_channel=source_channel,
        source_event_id=source_event_id,
        requested_enabled=enabled,
        requested_integer_value=None,
        expected_revision=expected_revision,
        provenance=provenance,
        timestamp=timestamp,
    )


def execute_owner_reply_grace_control_command(
    session: Session,
    *,
    tenant_id: str,
    command: OwnerReplyGraceControlCommand,
    authority: OperatorAuthority,
) -> OwnerReplyGraceControlMutation:
    if authority.tenant_id != tenant_id or not authority.can_issue_owner_commands:
        raise OwnerCommandUnauthorized("OWNER_COMMAND_REQUIRES_AUTHENTICATED_OPERATOR")
    represented = resolve_represented_subject(session, tenant_id)
    if represented is None or authority.operator_actor_id != represented.entity_id:
        raise OwnerCommandUnauthorized("OWNER_COMMAND_REQUIRES_REPRESENTED_OWNER")
    common = {
        "tenant_id": tenant_id,
        "represented_owner_actor_key": represented.entity_id,
        "policy_id": command.policy_id,
        "updated_by_actor_key": authority.operator_actor_id,
        "authorization_source": "OWNER_COMMAND",
        "source_channel": command.source_channel,
        "source_event_id": command.command_id,
        "provenance": {"command_type": command.command_type.value},
    }
    if command.command_type == OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_SECONDS:
        if not isinstance(command.value, int) or isinstance(command.value, bool):
            raise OperationalControlError("GRACE_SECONDS_MUST_BE_INTEGER")
        return set_owner_reply_grace_seconds(session, grace_seconds=command.value, **common)
    if command.command_type == OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_ENABLED:
        if not isinstance(command.value, bool):
            raise OperationalControlError("GRACE_ENABLED_MUST_BE_BOOLEAN")
        return set_owner_reply_grace_enabled(session, enabled=command.value, **common)
    raise OperationalControlError("OWNER_REPLY_GRACE_COMMAND_UNSUPPORTED")
