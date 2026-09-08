from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from attention_router.application.readiness_handoff import (
    ReadinessHandoffDenied,
    ReadinessHandoffRequest,
    prepare_from_fresh_readiness,
)


NOW = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)


def _request(**overrides):
    values = dict(
        tenant_id="tenant",
        run_id="run-1",
        scenario_version_id="version-1",
        synthetic_actor_binding_id="binding-1",
        source_sha="source",
        runtime_sha="runtime",
        schema_revision="0021_bounded_run_authorizations",
        driver_revision="driver",
        level="L1_SYNTHETIC_REAL_NETWORK",
        actor_scope="actor-synthetic",
        target_scope="actor-owner",
        capability_scope="conversation.reply",
        effect_scope="WHATSAPP_TEXT",
        authorized_by="operator:explicit-human-boundary",
        root_correlation_id="handoff-correlation-1",
    )
    values.update(overrides)
    return ReadinessHandoffRequest(**values)


def test_handoff_rechecks_freshness_before_inert_preparation(monkeypatch):
    calls = []
    auth_kwargs = {}
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_scenario_run",
        lambda *args, **kwargs: SimpleNamespace(id="run-1"),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.provision_scenario_execution_safety_in_transaction",
        lambda *args, **kwargs: SimpleNamespace(system_budget_id="budget-1", system_lease_id="lease-1"),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_bounded_authorization",
        lambda *args, **kwargs: (auth_kwargs.update(kwargs) or SimpleNamespace(id="auth-1")),
    )
    readiness = SimpleNamespace(
        id="readiness-1", state="READY", evaluated_at=NOW,
        evidence_fresh_until=NOW + timedelta(seconds=1),
        tenant_id="tenant", dimension="DOMAIN_READINESS", is_current=True,
    )
    session = SimpleNamespace(commit=lambda: calls.append("commit"), rollback=lambda: calls.append("rollback"))
    result = prepare_from_fresh_readiness(
        session, manifest=SimpleNamespace(safety=SimpleNamespace(allowed_target_scope={"actor_ref": "OWNER"})),
        request=_request(), reevaluate_once=lambda _: readiness, now=NOW + timedelta(milliseconds=100),
    )
    assert result.bounded_authorization_id == "auth-1"
    assert calls == ["commit"]
    assert (auth_kwargs["expires_at"] - auth_kwargs["valid_from"]).total_seconds() == 900


def test_handoff_creates_one_authorization_per_effect(monkeypatch):
    created = []
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_scenario_run",
        lambda *args, **kwargs: SimpleNamespace(id="run-1"),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.provision_scenario_execution_safety_in_transaction",
        lambda *args, **kwargs: SimpleNamespace(
            system_budget_id="text-budget", system_lease_id="text-lease",
            stimulus_budget_id="stimulus-budget", stimulus_lease_id="stimulus-lease",
        ),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_bounded_authorization",
        lambda *args, **kwargs: (created.append(kwargs) or SimpleNamespace(id=f"auth-{len(created)}")),
    )
    session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    result = prepare_from_fresh_readiness(
        session,
        manifest=SimpleNamespace(safety=SimpleNamespace(allowed_target_scope={"actor_ref": "OWNER"})),
        request=_request(), reevaluate_once=lambda _: SimpleNamespace(
            id="readiness-1", state="READY", evaluated_at=NOW,
            evidence_fresh_until=NOW + timedelta(seconds=1), tenant_id="tenant",
            dimension="DOMAIN_READINESS", is_current=True,
        ),
        now=NOW + timedelta(milliseconds=100),
    )
    assert result.bounded_authorization_ids == ("auth-1", "auth-2")
    assert {item["effect_scope"] for item in created} == {"WHATSAPP_TEXT", "WHATSAPP_STIMULUS"}
    assert {item["capability_scope"] for item in created} == {"conversation.reply", "synthetic_send_bounded"}


def test_handoff_expired_before_preparation_does_not_mutate():
    calls = []
    readiness = SimpleNamespace(
        id="readiness-1", state="READY", evaluated_at=NOW,
        evidence_fresh_until=NOW + timedelta(seconds=1),
        tenant_id="tenant", dimension="DOMAIN_READINESS", is_current=True,
    )
    session = SimpleNamespace(commit=lambda: calls.append("commit"), rollback=lambda: calls.append("rollback"))
    with pytest.raises(ReadinessHandoffDenied, match="EXPIRED_BEFORE_PREPARATION"):
        prepare_from_fresh_readiness(
            session, manifest=SimpleNamespace(safety=SimpleNamespace(allowed_target_scope={"actor_ref": "OWNER"})),
            request=_request(), reevaluate_once=lambda _: readiness, now=NOW + timedelta(seconds=1),
        )
    assert calls == []


def _install_preparation_stubs(monkeypatch, calls):
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_scenario_run",
        lambda *args, **kwargs: (calls.append("run") or SimpleNamespace(id="run-1")),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.provision_scenario_execution_safety_in_transaction",
        lambda *args, **kwargs: (
            calls.append("safety")
            or SimpleNamespace(system_budget_id="budget-1", system_lease_id="lease-1")
        ),
    )
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_bounded_authorization",
        lambda *args, **kwargs: (calls.append("auth") or SimpleNamespace(id="auth-1")),
    )


def _readiness(**overrides):
    values = dict(
        id="readiness-1",
        state="READY",
        evaluated_at=NOW,
        evidence_fresh_until=NOW + timedelta(seconds=1),
        tenant_id="tenant",
        dimension="DOMAIN_READINESS",
        is_current=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _session(calls):
    return SimpleNamespace(
        commit=lambda: calls.append("commit"),
        rollback=lambda: calls.append("rollback"),
    )


def _manifest():
    return SimpleNamespace(safety=SimpleNamespace(allowed_target_scope={"actor_ref": "OWNER"}))


@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN"])
def test_non_ready_readiness_fail_closes_before_preparation(state):
    calls = []
    with pytest.raises(ReadinessHandoffDenied, match="READINESS_NOT_READY"):
        prepare_from_fresh_readiness(
            _session(calls), manifest=_manifest(), request=_request(),
            reevaluate_once=lambda _: _readiness(state=state), now=NOW,
        )
    assert calls == []


def test_zero_window_fail_closes_before_preparation():
    calls = []
    with pytest.raises(ReadinessHandoffDenied, match="EXPIRED_BEFORE_PREPARATION"):
        prepare_from_fresh_readiness(
            _session(calls), manifest=_manifest(), request=_request(),
            reevaluate_once=lambda _: _readiness(evidence_fresh_until=NOW), now=NOW,
        )
    assert calls == []


def test_preparation_failure_rolls_back_entire_set(monkeypatch):
    calls = []
    _install_preparation_stubs(monkeypatch, calls)
    monkeypatch.setattr(
        "attention_router.application.readiness_handoff.create_bounded_authorization",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("auth failure")),
    )
    with pytest.raises(RuntimeError, match="auth failure"):
        prepare_from_fresh_readiness(
            _session(calls), manifest=_manifest(), request=_request(),
            reevaluate_once=lambda _: _readiness(), now=NOW,
        )
    assert calls == ["run", "safety", "rollback"]


def test_handoff_rejects_noncanonical_authorization_window():
    calls = []
    with pytest.raises(ReadinessHandoffDenied, match="OVERRIDE_FORBIDDEN"):
        prepare_from_fresh_readiness(
            _session(calls), manifest=_manifest(),
            request=_request(authorization_expires_at=NOW + timedelta(minutes=1)),
            reevaluate_once=lambda _: _readiness(), now=NOW,
        )
    assert calls == []
