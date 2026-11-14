"""Calendar-aware persistent grace, with no external providers or sends."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseStatus, parse_owner_grace_control,
)
from attention_router.application import autonomy, services
from attention_router.application.owner_control import (
    GracePeriodParameters, OwnerControlAction, dispatch_owner_control_action,
)
from attention_router.application.owner_operational_control import (
    OperationalControlConflict, OperationalControlError,
    set_owner_reply_grace_period,
)
from attention_router.application.owner_reply_grace import process_due_grace_windows
from attention_router.application.owner_control_semantic_candidates import (
    interpret_owner_control_candidates,
)
from attention_router.application.owner_control_clarification import (
    render_owner_control_clarification,
)
from attention_router.application.owner_temporal_duration import (
    TEMPORAL_PERIOD_PROVENANCE_FIELD,
    TemporalUnit, deadline_for_period,
)
from attention_router.config import settings
from attention_router.core.events import OperatorAuthority
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    InboundEventRow, OwnerOperationalControlRow, QueueRow,
)
from tests.test_owner_reply_grace_control import (
    OWNER_ACTOR_ID, _install_canary, _inbound, _manual, _set_enabled, _set_seconds,
    _window,
)


@pytest.fixture()
def control_canary(session):
    _install_canary(session)
    return session


def local(y=2028, mo=1, d=31):
    return datetime(y, mo, d, 21, 16, tzinfo=ZoneInfo("America/Fortaleza"))


def set_period(db, quantity, unit, event, stamp):
    return set_owner_reply_grace_period(
        db, tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
        quantity=quantity, unit=unit, timezone_name="America/Fortaleza",
        updated_by_actor_key=OWNER_ACTOR_ID,
        authorization_source="CONTROL_PANEL",
        source_channel="temporal-unit-test", source_event_id=event,
        timestamp=stamp,
    )


def test_flag_off_keeps_policy_unmodified(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", False)
    with pytest.raises(OperationalControlError, match="TEMPORAL_WAIT_DISABLED"):
        set_period(control_canary, 1, "YEAR", "off", local())
    assert control_canary.scalar(select(OwnerOperationalControlRow)) is None


@pytest.mark.parametrize(("amount", "unit", "start", "target"), [
    (20, "MINUTE", local(), local() + timedelta(minutes=20)),
    (1, "HOUR", local(), local() + timedelta(hours=1)),
    (1, "DAY", local(), local() + timedelta(days=1)),
    (1, "MONTH", local(2028, 1, 31), local(2028, 2, 29)),
    (1, "YEAR", local(2028, 2, 29), local(2029, 2, 28)),
])
def test_owner_control_creates_true_persistent_calendar_deadline(
    control_canary, monkeypatch, amount, unit, start, target,
):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    record = set_period(control_canary, amount, unit, "one", start)
    assert record.control.enabled
    assert record.control.temporal_period == {
        "quantity": amount, "unit": unit, "timezone_name": "America/Fortaleza"
    }
    services.receive_normalized_inbound_event(
        control_canary, _inbound("calendar-event", start)
    )
    window = _window(control_canary)
    assert window.state == "OPEN"
    assert window.auto_release_enabled
    assert window.due_at.replace(tzinfo=timezone.utc) == target.astimezone(timezone.utc)
    assert window.provenance[TEMPORAL_PERIOD_PROVENANCE_FIELD]["unit"] == unit


def test_change_recalculates_existing_window_without_erasing_history(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    services.receive_normalized_inbound_event(control_canary, _inbound("before-period", stamp))
    record = set_period(control_canary, 1, "MONTH", "update", stamp + timedelta(minutes=1))
    assert record.affected_open_windows == 1
    window = _window(control_canary)
    assert window.due_at.replace(tzinfo=timezone.utc) == local(2028, 2, 29).astimezone(timezone.utc)
    assert window.provenance[TEMPORAL_PERIOD_PROVENANCE_FIELD]["unit"] == "MONTH"


def test_legacy_seconds_reset_temporal_period(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "long", stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("first", stamp))
    _set_seconds(control_canary, 20, "reset", timestamp=stamp + timedelta(seconds=1))
    window = _window(control_canary)
    assert window.effective_grace_seconds == 20
    assert TEMPORAL_PERIOD_PROVENANCE_FIELD not in (window.provenance or {})
    assert window.due_at.replace(tzinfo=timezone.utc) == stamp.astimezone(timezone.utc) + timedelta(seconds=20)


def test_toggling_wait_preserves_period_without_mutating_owner_automation(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "first", stamp)
    disabled = _set_enabled(control_canary, False, "disabled", timestamp=stamp)
    assert not disabled.control.enabled
    assert disabled.control.temporal_period["unit"] == "YEAR"
    enabled = _set_enabled(control_canary, True, "enabled", timestamp=stamp)
    assert enabled.control.enabled
    assert enabled.control.temporal_period["unit"] == "YEAR"


def test_second_identical_control_event_is_idempotent(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    first = set_period(control_canary, 1, "YEAR", "idempotent", stamp)
    second = set_period(control_canary, 1, "YEAR", "idempotent", stamp)
    assert first.control.control_revision == second.control.control_revision
    assert second.duplicate


def test_same_event_different_period_is_rejected(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 60, "MINUTE", "idempotent", stamp)
    with pytest.raises(OperationalControlConflict, match="IDEMPOTENCY_CONFLICT"):
        set_period(control_canary, 1, "HOUR", "idempotent", stamp)


def test_worker_releases_only_on_deadline(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "long", stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("year-wait", stamp))
    window = _window(control_canary)
    due = window.due_at.replace(tzinfo=timezone.utc)
    assert process_due_grace_windows(control_canary, "worker", timestamp=due-timedelta(seconds=1)) == 0
    assert process_due_grace_windows(control_canary, "worker", timestamp=due+timedelta(seconds=1)) == 1
    assert window.state == "RELEASED"
    assert control_canary.scalar(select(QueueRow)) is not None
    assert process_due_grace_windows(control_canary, "worker", timestamp=due+timedelta(seconds=2)) == 0


def test_overdue_year_during_outage_cancels_instead_of_replaying(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "long", stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("outage", stamp))
    window = _window(control_canary)
    due = window.due_at.replace(tzinfo=timezone.utc)
    assert process_due_grace_windows(control_canary, "worker", timestamp=due+timedelta(hours=1)) == 0
    assert window.state == "CANCELED"
    assert window.cancellation_reason == "TEMPORAL_WAIT_RELEASE_TOO_LATE"
    assert control_canary.scalar(select(QueueRow)) is None


def test_manual_response_cancels_during_calendar_wait(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "long", stamp)
    services.receive_normalized_inbound_event(control_canary, _inbound("before-human", stamp))
    services.receive_normalized_inbound_event(control_canary, _manual("human", stamp+timedelta(days=1)))
    window = _window(control_canary)
    assert window.state == "CANCELED"
    due = deadline_for_period(stamp, {"quantity":1,"unit":"YEAR","timezone_name":"America/Fortaleza"})
    assert process_due_grace_windows(control_canary, "worker", timestamp=due) == 0


def test_command_and_clarification_distinguish_global_from_one_shot(monkeypatch):
    explicit = parse_owner_grace_control("Configure a espera padrão para um ano")
    assert explicit.status == OwnerControlParseStatus.MATCHED
    assert explicit.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_PERIOD
    assert isinstance(explicit.parameters, GracePeriodParameters)
    assert explicit.parameters.quantity == 1
    assert explicit.parameters.unit == TemporalUnit.YEAR
    pending = interpret_owner_control_candidates("Andy, aguarde um ano")
    assert {x["semantic_intent_key"] for x in pending["candidates"]} == {
        "CONFIGURE_OWNER_REPLY_GRACE_PERIOD", "ONE_SHOT_REPLY_DELAY_PERIOD"
    }
    prompt = render_owner_control_clarification(pending)
    assert "1 ano" in prompt
    assert "indisponível" in prompt


def test_seven_days_to_respond_requests_clarification_not_global_write():
    pending = interpret_owner_control_candidates("aguarde 7 dias para responder")
    assert pending is not None
    candidates = pending["candidates"]
    assert {item["semantic_intent_key"] for item in candidates} == {
        "CONFIGURE_OWNER_REPLY_GRACE_PERIOD",
        "ONE_SHOT_REPLY_DELAY_PERIOD",
    }
    assert all(item["parameters"] == {"quantity": 7, "unit": "DAY"}
               for item in candidates)
    response = render_owner_control_clarification(pending)
    assert "7 dias" in response
    assert "indisponível" in response


def test_authorized_owner_dispatch_under_feature_gate(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    monkeypatch.setattr(settings, "owner_temporal_timezone", "America/Fortaleza")
    authority=OperatorAuthority(
        tenant_id=DEFAULT_TENANT_ID,
        operator_actor_id=OWNER_ACTOR_ID,
        authenticated=True,
        roles=["OWNER"],
    )
    result=dispatch_owner_control_action(
        control_canary,
        tenant_id=DEFAULT_TENANT_ID,
        owner_actor_key=OWNER_ACTOR_ID,
        action=OwnerControlAction.SET_OWNER_REPLY_GRACE_PERIOD,
        parameters=GracePeriodParameters(1,TemporalUnit.YEAR),
        authority=authority,
        source_channel="android-client-command",
        source_event_id="temporal-client-command",
    )
    assert result.mutation.control.temporal_period["unit"]=="YEAR"


def test_fake_or_malformed_global_period_is_fail_closed(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp=local()
    set_period(control_canary, 1, "YEAR", "valid", stamp)
    row=control_canary.scalar(select(OwnerOperationalControlRow))
    row.provenance={**(row.provenance or {}),TEMPORAL_PERIOD_PROVENANCE_FIELD:{
        "quantity":1,"unit":"MONTH","timezone_name":"America/Fortaleza"}}
    from attention_router.application.owner_operational_control import resolve_global_owner_reply_grace_control
    effective=resolve_global_owner_reply_grace_control(
        control_canary,tenant_id=DEFAULT_TENANT_ID,represented_owner_actor_key=OWNER_ACTOR_ID
    )
    assert not effective.enabled
    assert effective.temporal_period is None


def test_freshness_exception_requires_exact_recent_authorized_release(
    control_canary, monkeypatch,
):
    """A year-old inbound can proceed only after its own completed wait."""
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    now = datetime.now(timezone.utc)
    received = now.replace(year=now.year - 1) - timedelta(seconds=2)
    monkeypatch.setattr(settings, "autonomous_decision_max_age_seconds", 300)
    monkeypatch.setattr(
        settings, "autonomous_execution_activated_at",
        (received - timedelta(days=1)).isoformat(),
    )
    set_period(control_canary, 1, "YEAR", "freshness", received)
    services.receive_normalized_inbound_event(
        control_canary, _inbound("freshness-year", received)
    )
    window = _window(control_canary)
    event = control_canary.get(InboundEventRow, window.anchor_event_id)
    assert not autonomy._fresh(event)
    assert not autonomy._fresh(event, session=control_canary)  # open, not yet released
    due = window.due_at.replace(tzinfo=timezone.utc)
    assert due <= datetime.now(timezone.utc)
    assert process_due_grace_windows(control_canary, "worker", timestamp=due) == 1
    assert window.state == "RELEASED"
    assert autonomy._fresh(event, session=control_canary)
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", False)
    assert not autonomy._fresh(event, session=control_canary)


def test_freshness_cannot_reuse_release_from_other_event(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    now = datetime.now(timezone.utc)
    received = now.replace(year=now.year - 1) - timedelta(seconds=2)
    monkeypatch.setattr(
        settings, "autonomous_execution_activated_at",
        (received - timedelta(days=1)).isoformat(),
    )
    set_period(control_canary, 1, "YEAR", "freshness", received)
    services.receive_normalized_inbound_event(control_canary, _inbound("my-event", received))
    window = _window(control_canary)
    event = control_canary.get(InboundEventRow, window.anchor_event_id)
    due = window.due_at.replace(tzinfo=timezone.utc)
    assert process_due_grace_windows(control_canary, "worker", timestamp=due) == 1
    assert autonomy._fresh(event, session=control_canary)
    window.claimed_generation += 1
    assert not autonomy._fresh(event, session=control_canary)


def test_older_than_activation_watermark_stays_rejected(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    now=datetime.now(timezone.utc)
    received=now.replace(year=now.year-1)-timedelta(seconds=2)
    monkeypatch.setattr(settings,"autonomous_execution_activated_at",
                        (received+timedelta(days=1)).isoformat())
    set_period(control_canary,1,"YEAR","freshness",received)
    services.receive_normalized_inbound_event(control_canary,_inbound("old-event",received))
    window=_window(control_canary)
    assert process_due_grace_windows(control_canary,"worker",timestamp=window.due_at) == 1
    event=control_canary.get(InboundEventRow,window.anchor_event_id)
    assert not autonomy._fresh(event,session=control_canary)


def test_no_freshness_for_manufactured_grace_period(control_canary, monkeypatch):
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    now=datetime.now(timezone.utc)
    received=now.replace(year=now.year-1)-timedelta(seconds=2)
    monkeypatch.setattr(settings,"autonomous_execution_activated_at",
                        (received-timedelta(days=1)).isoformat())
    set_period(control_canary,1,"YEAR","freshness",received)
    services.receive_normalized_inbound_event(control_canary,_inbound("tamper",received))
    window=_window(control_canary)
    assert process_due_grace_windows(control_canary,"worker",timestamp=window.due_at) == 1
    event=control_canary.get(InboundEventRow,window.anchor_event_id)
    assert autonomy._fresh(event,session=control_canary)
    window.provenance={**window.provenance,TEMPORAL_PERIOD_PROVENANCE_FIELD:{
        "quantity":2,"unit":"YEAR","timezone_name":"America/Fortaleza"}}
    assert not autonomy._fresh(event,session=control_canary)



def test_disabling_feature_after_configuration_fails_closed(control_canary, monkeypatch):
    from attention_router.application.owner_operational_control import (
        resolve_global_owner_reply_grace_control,
    )
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", True)
    stamp = local()
    set_period(control_canary, 1, "YEAR", "configure", stamp)
    monkeypatch.setattr(settings, "owner_temporal_wait_enabled", False)
    resolved = resolve_global_owner_reply_grace_control(
        control_canary, tenant_id=DEFAULT_TENANT_ID,
        represented_owner_actor_key=OWNER_ACTOR_ID,
    )
    assert not resolved.enabled
    assert resolved.temporal_period is None
