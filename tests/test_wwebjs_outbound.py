import json
from datetime import timedelta
from io import BytesIO
from urllib import error

import pytest
from sqlalchemy import select

import attention_router.adapters.wwebjs_outbound as wwebjs_adapter_module
from attention_router.adapters.wwebjs_outbound import (
    WwebjsOutboundAdapter,
    WwebjsOutboundError,
    WwebjsOutboundPermanentError,
    _signature,
)
from attention_router.application import services
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import AuditEventRow, OutboxMessageRow
from attention_router.infrastructure.repository import upsert_actor_binding


class FakeWwebjsAdapter:
    def __init__(self, result=None, exc=None):
        self.result = result or type("Result", (), {"status": "sent"})()
        self.exc = exc
        self.calls = []

    def dispatch_outbox(self, outbox):
        self.calls.append(outbox.idempotency_key)
        if self.exc:
            raise self.exc
        return self.result


def add_wwebjs_interaction(session):
    result = services.receive_inbound_event(
        session,
        "wwebjs",
        f"evt-{new_id()}",
        "message",
        "actor-key",
        "Actor",
        "unknown",
        None,
        "Fixture inbound.",
        payload={
            "schema_version": "1",
            "source": "wwebjs",
            "external_event_id": "wamid.fixture",
            "external_actor_id": "5500000000029@c.us",
            "content": "Fixture inbound.",
        },
    )
    session.commit()
    return result


def test_wwebjs_adapter_serialization(session):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    body, headers = WwebjsOutboundAdapter(secret="x" * 32).build_request(outbox)
    payload = json.loads(body.decode())
    assert payload["idempotency_key"] == outbox.idempotency_key
    assert payload["external_actor_id"].endswith("@c.us")
    assert payload["message_type"] == "text"
    assert headers["X-Attention-Signature"].startswith("sha256=")


def test_wwebjs_adapter_hmac_signature(session):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    adapter = WwebjsOutboundAdapter(secret="x" * 32)
    body, headers = adapter.build_request(outbox)
    assert headers["X-Attention-Signature"] == _signature("x" * 32, headers["X-Attention-Timestamp"], body)


def test_wwebjs_ambiguous_outcome_is_not_replayed(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    key = outbox.idempotency_key
    fake = FakeWwebjsAdapter(exc=WwebjsOutboundError("not_ready"))
    monkeypatch.setattr(services, "wwebjs_outbound", fake)
    services.process_outbox(session, "worker-a")
    session.commit()
    outbox.available_at = now_utc() - timedelta(seconds=1)
    services.process_outbox(session, "worker-b")
    assert fake.calls == [key]
    assert outbox.status == "AMBIGUOUS"


def test_stale_external_processing_is_quarantined_before_dispatch(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(
        session,
        result["id"],
        "Teste controlado.",
    )
    outbox.status = "PROCESSING"
    outbox.claimed_at = now_utc() - timedelta(
        seconds=settings.worker_timer_lease_seconds + 1
    )
    outbox.claimed_by = "stale-worker"
    outbox.attempt_count = 1
    session.commit()
    fake = FakeWwebjsAdapter()
    monkeypatch.setattr(services, "wwebjs_outbound", fake)

    assert services.process_outbox(session, "replacement-worker") == 0

    session.refresh(outbox)
    assert outbox.status == "AMBIGUOUS"
    assert outbox.last_error == "STALE_PROCESSING_OUTCOME_UNKNOWN"
    assert services.claim_outbox(session, "later-worker") == []
    assert fake.calls == []


@pytest.mark.parametrize("provider_error", ["network_timeout", "http_503"])
def test_wwebjs_ambiguous_transport_result_is_attempted_once_and_never_reclaimed(
    session,
    monkeypatch,
    provider_error,
):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(
        session,
        result["id"],
        "Teste controlado.",
    )
    adapter = WwebjsOutboundAdapter(secret="x" * 32)
    adapter_calls = []
    original_dispatch = adapter.dispatch_outbox

    def fail_transport(*args, **kwargs):
        del args, kwargs
        if provider_error == "network_timeout":
            raise TimeoutError("synthetic timeout")
        raise error.HTTPError(
            url="http://synthetic.invalid/internal/send",
            code=503,
            msg="synthetic unavailable",
            hdrs=None,
            fp=BytesIO(b'{"status":"not_ready"}'),
        )

    def tracked_dispatch(row):
        adapter_calls.append(row.idempotency_key)
        return original_dispatch(row)

    monkeypatch.setattr(wwebjs_adapter_module.request, "urlopen", fail_transport)
    monkeypatch.setattr(adapter, "dispatch_outbox", tracked_dispatch)
    monkeypatch.setattr(services, "wwebjs_outbound", adapter)

    assert services.process_outbox(session, "worker-a") == 1
    session.refresh(outbox)
    assert outbox.status == "AMBIGUOUS"
    assert outbox.attempt_count == 1

    assert services.process_outbox(session, "worker-b") == 0
    assert services.claim_outbox(session, "worker-c") == []
    assert adapter_calls == [outbox.idempotency_key]
    assert outbox.status != "RETRY"


def test_provider_confirmed_then_local_commit_failure_requires_reconciliation(
    session,
    monkeypatch,
):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_controlled_outbound(
        session,
        result["id"],
        "Teste controlado.",
    )
    fake = FakeWwebjsAdapter()
    monkeypatch.setattr(services, "wwebjs_outbound", fake)
    original_commit = session.commit
    commit_calls = 0

    def fail_provider_finalization_commit_once():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise RuntimeError("simulated local finalization commit failure")
        return original_commit()

    monkeypatch.setattr(session, "commit", fail_provider_finalization_commit_once)

    assert services.process_outbox(session, "worker-a") == 1
    session.refresh(outbox)
    assert outbox.status == "RECONCILIATION_REQUIRED"
    assert outbox.last_error == "PROVIDER_CONFIRMED_LOCAL_FINALIZATION_FAILED"
    assert outbox.attempt_count == 1

    assert services.process_outbox(session, "worker-b") == 0
    assert services.claim_outbox(session, "worker-c") == []
    assert fake.calls == [outbox.idempotency_key]
    assert outbox.status != "RETRY"


def test_wwebjs_200_sent_done(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter())
    services.process_outbox(session, "worker-a")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).one()
    assert outbox.status == "DONE"


def test_timeline_failure_after_transport_success_does_not_retry_send(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    fake = FakeWwebjsAdapter()
    monkeypatch.setattr(services, "wwebjs_outbound", fake)
    monkeypatch.setattr(
        services,
        "record_timeline_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timeline unavailable")),
    )

    services.process_outbox(session, "worker-a")

    outbox = session.scalars(
        select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")
    ).one()
    assert outbox.status == "DONE"
    assert outbox.attempt_count == 1
    assert len(fake.calls) == 1
    failure_audit = session.scalar(
        select(AuditEventRow).where(AuditEventRow.event_type == "timeline_record_failed")
    )
    assert failure_audit is not None


def test_wwebjs_already_sent_done(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter(type("Result", (), {"status": "already_sent"})()))
    services.process_outbox(session, "worker-a")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).one()
    assert outbox.status == "DONE"


def test_wwebjs_503_is_ambiguous_and_not_retryable(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter(exc=WwebjsOutboundError("not_ready")))
    services.process_outbox(session, "worker-a")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).one()
    assert outbox.status == "AMBIGUOUS"


def test_wwebjs_network_error_is_ambiguous_and_not_retryable(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter(exc=WwebjsOutboundError("network_error")))
    services.process_outbox(session, "worker-a")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).one()
    assert outbox.status == "AMBIGUOUS"


def test_wwebjs_409_is_quarantined_without_replay(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter(exc=WwebjsOutboundPermanentError("conflict")))
    services.process_outbox(session, "worker-a")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).one()
    assert outbox.status == "AMBIGUOUS"


def test_cell_phone_does_not_use_wwebjs(session, monkeypatch):
    result = services.create_interaction(session, "message", "contact_mae", "Contato", "family_core", None, "Fixture")
    fake = FakeWwebjsAdapter()
    monkeypatch.setattr(services, "wwebjs_outbound", fake)
    services.process_outbox(session, "worker-a")
    assert fake.calls == []
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])).first()
    assert outbox.destination == "cell_phone"


def test_soft_ping_does_not_use_wwebjs(session, monkeypatch):
    result = services.create_interaction(session, "message", "unknown", "Contato", "unknown", None, "Fixture")
    fake = FakeWwebjsAdapter()
    monkeypatch.setattr(services, "wwebjs_outbound", fake)
    services.process_outbox(session, "worker-a")
    assert fake.calls == []
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])).first()
    assert outbox.destination == "soft_ping"


def test_enqueue_controlado_gera_exatamente_um_outbox(session):
    result = add_wwebjs_interaction(session)
    before = len(session.scalars(select(OutboxMessageRow)).all())
    outbox = services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    after = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.id == outbox.id)).all()
    assert len(after) == 1
    assert len(session.scalars(select(OutboxMessageRow)).all()) == before + 1
    assert after[0].destination == "wwebjs"


def test_controlled_outbound_rejects_actor_when_test_not_allowed(session):
    result = add_wwebjs_interaction(session)
    upsert_actor_binding(
        session,
        source="wwebjs",
        external_actor_id="5500000000029@c.us",
        actor_key="actor_protected_fixture",
        actor_category="real_contact",
        display_name="Taylor",
        metadata={
            "actor_alias": "taylor",
            "display_name": "Taylor",
            "relationship": "contato",
            "role": "contact",
            "priority": "normal",
            "test_allowed": False,
            "policy_id": "attention_default_v1",
        },
    )
    before = len(session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).all())
    try:
        services.enqueue_wwebjs_controlled_outbound(session, result["id"], "Teste controlado.")
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("protected actor should be rejected for controlled test")
    after = len(session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.destination == "wwebjs")).all())
    assert after == before


def test_manual_reply_requires_wwebjs_individual_interaction(session):
    result = add_wwebjs_interaction(session)
    row, _receipt, actor = services.validate_wwebjs_manual_reply_target(session, result["id"])
    assert row.id == result["id"]
    assert actor.endswith("@c.us")


def test_manual_reply_enqueues_one_outbox_and_reuses_correlation(session):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta manual.")
    again = services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta manual.")
    rows = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.idempotency_key == outbox.idempotency_key)).all()
    assert again.id == outbox.id
    assert len(rows) == 1
    assert outbox.action_type == "wwebjs_manual_reply_text"
    assert outbox.interaction_id == result["id"]
    assert outbox.correlation_id == result["correlation_id"]


def test_manual_reply_same_idempotency_key_does_not_duplicate(session):
    result = add_wwebjs_interaction(session)
    first = services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta manual.", "manual-key")
    second = services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta diferente.", "manual-key")
    assert second.id == first.id
    assert len(session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.idempotency_key == "manual-key")).all()) == 1


def test_manual_reply_audit_does_not_store_full_text(session):
    result = add_wwebjs_interaction(session)
    services.enqueue_wwebjs_manual_reply(session, result["id"], "Texto sensivel completo.")
    audits = session.scalars(
        select(AuditEventRow).where(
            AuditEventRow.interaction_id == result["id"],
            AuditEventRow.event_type.in_(["manual_reply_requested", "manual_reply_enqueued"]),
        )
    ).all()
    assert {audit.event_type for audit in audits} == {"manual_reply_requested", "manual_reply_enqueued"}
    assert all("Texto sensivel completo." not in str(audit.payload) for audit in audits)


def test_manual_reply_group_rejected(session):
    result = services.receive_inbound_event(
        session,
        "wwebjs",
        f"evt-{new_id()}",
        "message",
        "group-key",
        "Group",
        "unknown",
        None,
        "Fixture inbound.",
        payload={
            "schema_version": "1",
            "source": "wwebjs",
            "external_event_id": "group.fixture",
            "external_actor_id": "contact002@example.com",
            "content": "Fixture inbound.",
            "metadata": {"is_group": True},
        },
    )
    try:
        services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta manual.")
    except ValueError as exc:
        assert "group" in str(exc)
    else:
        raise AssertionError("group interaction should be rejected")


def test_manual_reply_delivered_audit_on_worker_done(session, monkeypatch):
    result = add_wwebjs_interaction(session)
    outbox = services.enqueue_wwebjs_manual_reply(session, result["id"], "Resposta manual.")
    monkeypatch.setattr(services, "wwebjs_outbound", FakeWwebjsAdapter())
    services.process_outbox(session, "worker-a")
    delivered = session.scalars(
        select(AuditEventRow).where(AuditEventRow.interaction_id == result["id"], AuditEventRow.event_type == "manual_reply_delivered")
    ).one()
    assert delivered.payload == {"outbox_id": outbox.id, "ha_status": "sent"}
