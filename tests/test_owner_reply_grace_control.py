from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.application.owner_operational_control import (
    CONTROL_SOURCE_OWNER_OVERRIDE,
    OperationalControlConflict,
    OperationalControlError,
    OperationalControlUnauthorized,
    OwnerReplyGraceCommandType,
    OwnerReplyGraceControlCommand,
    execute_owner_reply_grace_control_command,
    get_owner_reply_grace_control,
    set_owner_reply_grace_enabled,
    set_owner_reply_grace_seconds,
)
from attention_router.application.owner_reply_grace import (
    CANARY_ACTOR_ID,
    CANARY_AUDIENCE,
    CANARY_BINDING_ID,
    CANARY_POLICY_ID,
    OWNER_MANUAL_OUTBOUND_OBSERVED,
    process_due_grace_windows,
)
from attention_router.application.platform.entities import create_relationship
from attention_router.core.entities import EntityReference
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import (
    ActorBindingRow,
    AuditEventRow,
    ConversationResponseGraceWindowRow,
    OwnerOperationalControlChangeRow,
    OwnerOperationalControlRow,
    PolicyRow,
    PolicyVersionRow,
    QueueRow,
)
from attention_router.infrastructure.repository import (
    ensure_policy_version,
    provision_morgan_owner_reply_grace_policy,
)


OWNER_ACTOR_ID = "actor_owner_control_test"
PEER_ID = "5500000000021@lid"
CONVERSATION = "wwebjs:5500000000021@lid"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _base_policy_config() -> dict:
    return {
        "identifier": CANARY_POLICY_ID,
        "name": "Morgan Presence Autonomy",
        "match_criteria": {
            "audience": CANARY_AUDIENCE,
            "binding_id": CANARY_BINDING_ID,
        },
        "priority": 2000,
        "specificity": 2000,
        "tone": "cordial",
        "initial_wait_seconds": 1,
        "allowed_disclosures": ["availability_hint"],
        "allowed_actions": ["respond"],
        "escalation_steps": ["respond"],
        "ack_timeout_seconds": 30,
        "repetition_limit": 1,
        "cancellation_conditions": ["human_reply"],
        "completion_conditions": ["safe_completion"],
        "execution_mode": "AUTO_ALLOWED",
        "execution_scope": {
            "actor_ids": [CANARY_ACTOR_ID],
            "binding_ids": [CANARY_BINDING_ID],
            "audiences": [CANARY_AUDIENCE],
        },
    }


def _install_canary(session: Session) -> None:
    stamp = now_utc()
    session.add_all(
        [
            ActorBindingRow(
                id="owner-control-binding",
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_actor_id="owner-control@c.us",
                actor_key=OWNER_ACTOR_ID,
                display_name="Alex",
                actor_category="owner",
                active_context=None,
                is_active=True,
                binding_metadata={"owner": True},
                created_at=stamp,
                updated_at=stamp,
            ),
            ActorBindingRow(
                id=CANARY_BINDING_ID,
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_actor_id=PEER_ID,
                actor_key=CANARY_ACTOR_ID,
                display_name="Sr. Morgan Example",
                actor_category="family_core",
                active_context=None,
                is_active=True,
                binding_metadata={"audience": CANARY_AUDIENCE, "canary": True},
                created_at=stamp,
                updated_at=stamp,
            ),
        ]
    )
    config = _base_policy_config()
    policy = PolicyRow(
        **{key: config[key] for key in PolicyRow.__table__.columns.keys() if key in config},
        tenant_id=DEFAULT_TENANT_ID,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    ensure_policy_version(session, policy, config, "control-test")
    create_relationship(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        source=EntityReference(entity_type="ACTOR", entity_id=CANARY_ACTOR_ID),
        target=EntityReference(entity_type="ACTOR", entity_id=OWNER_ACTOR_ID),
        relationship_type="pai",
        metadata_sanitized={"authorization": "explicit_human_authorization"},
    )
    provision_morgan_owner_reply_grace_policy(session, origin="control-test")
    session.flush()


@pytest.fixture()
def control_canary(session: Session) -> Session:
    _install_canary(session)
    return session


def _inbound(event_id: str, stamp: datetime) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs",
        external_event_id=event_id,
        event_type="message",
        occurred_at=stamp,
        received_at=stamp,
        actor_id=PEER_ID,
        actor_display_name="Sr. Morgan Example",
        actor_category="family_core",
        channel="whatsapp",
        content=f"Message {event_id}",
        event_origin="EXTERNAL_INBOUND",
        metadata={
            "conversation_key": CONVERSATION,
            "conversation_state": "READY",
            "source_account": "default",
            "peer_identifiers": [PEER_ID],
            "from_me": False,
        },
    )


def _manual(event_id: str, stamp: datetime) -> NormalizedInboundEvent:
    return NormalizedInboundEvent(
        source="wwebjs",
        external_event_id=event_id,
        event_type="message",
        occurred_at=stamp,
        received_at=stamp,
        actor_id=PEER_ID,
        actor_display_name="Owner outbound observation",
        actor_category="owner_outbound_observation",
        channel="whatsapp",
        content="[owner outbound observation]",
        event_origin=OWNER_MANUAL_OUTBOUND_OBSERVED,
        metadata={
            "conversation_key": CONVERSATION,
            "conversation_state": "READY",
            "source_account": "default",
            "peer_identifiers": [PEER_ID],
            "from_me": True,
            "from_me_classification": OWNER_MANUAL_OUTBOUND_OBSERVED,
        },
    )


def _window(session: Session) -> ConversationResponseGraceWindowRow:
    return session.scalar(select(ConversationResponseGraceWindowRow))


def _set_seconds(
    session: Session, seconds: int, command_id: str, *, timestamp: datetime | None = None
):
    return set_owner_reply_grace_seconds(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        policy_id=None,
        grace_seconds=seconds,
        updated_by_actor_key=OWNER_ACTOR_ID,
        authorization_source="CONTROL_PANEL",
        source_channel="control-test",
        source_event_id=command_id,
        timestamp=timestamp,
    )


def _set_enabled(
    session: Session, enabled: bool, command_id: str, *, timestamp: datetime | None = None
):
    return set_owner_reply_grace_enabled(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        policy_id=None,
        enabled=enabled,
        updated_by_actor_key=OWNER_ACTOR_ID,
        authorization_source="CONTROL_PANEL",
        source_channel="control-test",
        source_event_id=command_id,
        timestamp=timestamp,
    )


def test_potentiometer_01_no_override_uses_policy_default_30(control_canary):
    effective = get_owner_reply_grace_control(
        control_canary,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        policy_id=None,
    )
    assert effective.enabled is True
    assert effective.effective_seconds == 30
    assert effective.source == "GLOBAL_DEFAULT"
    assert effective.control_revision is None
    policy = control_canary.get(PolicyRow, CANARY_POLICY_ID)
    assert policy.current_version_id is not None
    version = control_canary.get(PolicyVersionRow, policy.current_version_id)
    assert version.config["owner_reply_grace_allowed"] is True
    assert version.config["owner_reply_grace_default_seconds"] == 30
    assert version.config["owner_reply_grace_min_seconds"] == 0
    assert version.config["owner_reply_grace_max_seconds"] == 300
    assert "owner_reply_grace_seconds" not in version.config


def test_potentiometer_02_override_60_applies_to_new_window(control_canary):
    stamp = now_utc()
    _set_seconds(control_canary, 60, "command-02", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-02", stamp))
    window = _window(control_canary)
    assert window.effective_grace_seconds == 60
    assert _utc(window.due_at) == _utc(stamp) + timedelta(seconds=60)
    assert window.operational_control_source == CONTROL_SOURCE_OWNER_OVERRIDE


def test_potentiometer_03_override_10_applies_to_new_window(control_canary):
    stamp = now_utc()
    _set_seconds(control_canary, 10, "command-03", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-03", stamp))
    window = _window(control_canary)
    assert window.effective_grace_seconds == 10
    assert _utc(window.due_at) == _utc(stamp) + timedelta(seconds=10)


def test_potentiometer_04_disabled_never_auto_releases(control_canary):
    stamp = now_utc()
    _set_enabled(control_canary, False, "command-04", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-04", stamp))
    window = _window(control_canary)
    assert window.state == "OPEN"
    assert window.auto_release_enabled is False
    assert process_due_grace_windows(
        control_canary, "worker", timestamp=stamp + timedelta(days=1)
    ) == 0
    assert control_canary.scalar(select(func.count()).select_from(QueueRow)) == 0


def test_potentiometer_05_reenable_restores_async_release(control_canary):
    stamp = now_utc()
    _set_enabled(control_canary, False, "command-05-off", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-05", stamp))
    changed = _set_enabled(
        control_canary, True, "command-05-on", timestamp=stamp + timedelta(seconds=10)
    )
    assert changed.affected_open_windows == 1
    assert process_due_grace_windows(
        control_canary, "worker", timestamp=stamp + timedelta(seconds=30)
    ) == 1


def test_potentiometer_06_zero_is_immediate_worker_eligibility_not_disabled(control_canary):
    stamp = now_utc()
    _set_seconds(control_canary, 0, "command-06-zero", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-06", stamp))
    window = _window(control_canary)
    assert window.auto_release_enabled is True
    assert _utc(window.due_at) == _utc(stamp)
    assert control_canary.scalar(select(func.count()).select_from(QueueRow)) == 0
    assert process_due_grace_windows(control_canary, "worker", timestamp=stamp) == 1


def test_potentiometer_07_below_policy_minimum_is_rejected(control_canary):
    with pytest.raises(OperationalControlError, match="OUT_OF_POLICY_BOUNDS"):
        _set_seconds(control_canary, -1, "command-07")
    assert control_canary.scalar(select(func.count()).select_from(OwnerOperationalControlRow)) == 0


def test_potentiometer_08_above_policy_maximum_is_rejected(control_canary):
    with pytest.raises(OperationalControlError, match="OUT_OF_POLICY_BOUNDS"):
        _set_seconds(control_canary, 301, "command-08")
    assert control_canary.scalar(select(func.count()).select_from(OwnerOperationalControlRow)) == 0


def test_potentiometer_09_change_30_to_60_recalculates_open_window(control_canary):
    stamp = now_utc()
    services.receive_normalized_inbound_event(control_canary, _inbound("control-09", stamp))
    changed = _set_seconds(
        control_canary, 60, "command-09", timestamp=stamp + timedelta(seconds=10)
    )
    window = _window(control_canary)
    assert changed.affected_open_windows == 1
    assert _utc(window.due_at) == _utc(window.last_inbound_at) + timedelta(seconds=60)
    assert window.operational_control_revision == 1


def test_potentiometer_10_change_60_to_10_recalculates_open_window(control_canary):
    stamp = now_utc()
    _set_seconds(control_canary, 60, "command-10-60", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-10", stamp))
    changed = _set_seconds(
        control_canary, 10, "command-10-10", timestamp=stamp + timedelta(seconds=5)
    )
    window = _window(control_canary)
    assert changed.affected_open_windows == 1
    assert _utc(window.due_at) == _utc(window.last_inbound_at) + timedelta(seconds=10)
    assert window.operational_control_revision == 2


def test_potentiometer_11_past_deadline_is_only_worker_eligible(control_canary):
    stamp = now_utc()
    _set_seconds(control_canary, 60, "command-11-60", timestamp=stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("control-11", stamp))
    _set_seconds(
        control_canary, 10, "command-11-10", timestamp=stamp + timedelta(seconds=20)
    )
    assert control_canary.scalar(select(func.count()).select_from(QueueRow)) == 0
    assert process_due_grace_windows(
        control_canary, "worker", timestamp=stamp + timedelta(seconds=20)
    ) == 1


def test_potentiometer_12_released_window_is_not_retroactive(control_canary):
    stamp = now_utc()
    services.receive_normalized_inbound_event(control_canary, _inbound("control-12", stamp))
    process_due_grace_windows(
        control_canary, "worker", timestamp=stamp + timedelta(seconds=30)
    )
    window = _window(control_canary)
    original_due = _utc(window.due_at)
    changed = _set_seconds(
        control_canary, 60, "command-12", timestamp=stamp + timedelta(seconds=31)
    )
    assert changed.affected_open_windows == 0
    assert window.state == "RELEASED"
    assert _utc(window.due_at) == original_due
    assert window.effective_grace_seconds == 30


def test_potentiometer_13_canceled_window_is_not_retroactive(control_canary):
    stamp = now_utc()
    services.receive_normalized_inbound_event(control_canary, _inbound("control-13", stamp))
    services.receive_normalized_inbound_event(
        control_canary, _manual("control-13-owner", stamp + timedelta(seconds=1))
    )
    window = _window(control_canary)
    original_due = _utc(window.due_at)
    changed = _set_seconds(
        control_canary, 60, "command-13", timestamp=stamp + timedelta(seconds=2)
    )
    assert changed.affected_open_windows == 0
    assert window.state == "CANCELED"
    assert _utc(window.due_at) == original_due


def test_potentiometer_14_duplicate_owner_command_is_idempotent(control_canary):
    authority = OperatorAuthority(
        tenant_id=DEFAULT_TENANT_ID,
        operator_actor_id=OWNER_ACTOR_ID,
        authenticated=True,
        roles=["OWNER"],
    )
    command = OwnerReplyGraceControlCommand(
        command_id="owner-command-14",
        command_type=OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_SECONDS,
        value=60,
        policy_id=None,
        source_channel="wwebjs-owner-command",
    )
    first = execute_owner_reply_grace_control_command(
        control_canary, tenant_id=DEFAULT_TENANT_ID, command=command, authority=authority
    )
    second = execute_owner_reply_grace_control_command(
        control_canary, tenant_id=DEFAULT_TENANT_ID, command=command, authority=authority
    )
    assert first.control.control_revision == second.control.control_revision == 1
    assert second.duplicate is True
    assert control_canary.scalar(
        select(func.count()).select_from(OwnerOperationalControlChangeRow)
    ) == 1


def test_potentiometer_15_non_owner_command_is_rejected(control_canary):
    authority = OperatorAuthority(
        tenant_id=DEFAULT_TENANT_ID,
        operator_actor_id="actor_not_owner",
        authenticated=True,
        roles=["OWNER"],
    )
    command = OwnerReplyGraceControlCommand(
        command_id="owner-command-15",
        command_type=OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_ENABLED,
        value=False,
        policy_id=None,
        source_channel="wwebjs-owner-command",
    )
    with pytest.raises(OwnerCommandUnauthorized, match="REPRESENTED_OWNER"):
        execute_owner_reply_grace_control_command(
            control_canary,
            tenant_id=DEFAULT_TENANT_ID,
            command=command,
            authority=authority,
        )
    assert control_canary.scalar(select(func.count()).select_from(OwnerOperationalControlRow)) == 0


def test_potentiometer_16_channel_independent_service_supports_panel(control_canary):
    result = set_owner_reply_grace_seconds(
        control_canary,
        tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        policy_id=None,
        grace_seconds=60,
        updated_by_actor_key=OWNER_ACTOR_ID,
        authorization_source="CONTROL_PANEL",
        source_channel="future-panel",
        source_event_id="panel-command-16",
    )
    row = control_canary.scalar(select(OwnerOperationalControlRow))
    assert result.control.effective_seconds == 60
    assert row.source_channel == "future-panel"
    assert row.authorization_source == "CONTROL_PANEL"


def test_potentiometer_20_restart_preserves_control_and_recalculated_deadline(control_canary):
    stamp = now_utc()
    services.receive_normalized_inbound_event(control_canary, _inbound("control-20", stamp))
    _set_seconds(
        control_canary, 60, "command-20", timestamp=stamp + timedelta(seconds=5)
    )
    control_canary.commit()
    with Session(bind=control_canary.bind, expire_on_commit=False) as restarted:
        control = get_owner_reply_grace_control(
            restarted,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER_ACTOR_ID,
            policy_id=None,
        )
        window = restarted.scalar(select(ConversationResponseGraceWindowRow))
        assert control.effective_seconds == 60
        assert control.control_revision == 1
        assert _utc(window.due_at) == _utc(window.last_inbound_at) + timedelta(seconds=60)


def test_control_revision_conflict_is_rejected(control_canary):
    first = _set_seconds(control_canary, 60, "revision-first")
    with pytest.raises(OperationalControlConflict, match="REVISION_CONFLICT"):
        set_owner_reply_grace_seconds(
            control_canary,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key=OWNER_ACTOR_ID,
            policy_id=None,
            grace_seconds=10,
            updated_by_actor_key=OWNER_ACTOR_ID,
            authorization_source="CONTROL_PANEL",
            source_channel="control-test",
            source_event_id="revision-stale",
            expected_revision=0,
        )
    assert first.control.control_revision == 1


def test_control_read_rejects_noncanonical_represented_owner(control_canary):
    with pytest.raises(OperationalControlUnauthorized, match="REPRESENTED_OWNER_UNRESOLVED"):
        get_owner_reply_grace_control(
            control_canary,
            tenant_id=DEFAULT_TENANT_ID,
            represented_owner_actor_key="actor_not_owner",
            policy_id=None,
        )


def test_control_change_audits_values_revisions_and_affected_windows(control_canary):
    stamp = now_utc()
    services.receive_normalized_inbound_event(control_canary, _inbound("control-audit", stamp))
    _set_seconds(
        control_canary, 60, "audit-command", timestamp=stamp + timedelta(seconds=1)
    )
    event_types = set(
        control_canary.scalars(
            select(AuditEventRow.event_type).where(
                AuditEventRow.origin == "owner_operational_control"
            )
        ).all()
    )
    assert {
        "operational_control.changed",
        "owner_reply_grace.seconds_changed",
        "grace.deadline_recalculated",
    } <= event_types
