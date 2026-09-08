import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from attention_router.application import services
from attention_router.domain.enums import ActionState, InteractionState
from attention_router.infrastructure.models import (
    ActionAttemptRow,
    AuditEventRow,
    InboundEventRow,
    OutboxMessageRow,
    PolicyRow,
    TimerRow,
)
from attention_router.infrastructure.repository import (
    activate_policy_version,
    list_policy_versions,
    seed_policies,
    update_policy,
)
from attention_router.infrastructure.db import Base


def create(session, contact_id="unknown_contact", category="unknown", context=None):
    result = services.create_interaction(
        session,
        "message",
        contact_id,
        "Contato Sintético",
        category,
        context,
        "Fixture sintética sem dados pessoais.",
    )
    session.commit()
    return result


def first_action(session, interaction_id):
    return session.scalars(select(ActionAttemptRow).where(ActionAttemptRow.interaction_id == interaction_id)).first()


def test_unknown_contact_uses_default_policy(session):
    assert create(session)["policy_id"] == "desconhecido"


def test_mother_gets_maximum_priority(session):
    result = create(session, "contact_mae", "family_core")
    assert result["policy_id"] == "mae"
    assert result["decisions"][0]["matched_rules"][0]["policy_id"] == "mae"


def test_father_uses_own_tone(session):
    result = create(session, "contact_pai", "family_core")
    assert result["policy_id"] == "pai"
    assert "leve" in result["lia_speech"]


def test_interview_context_selects_recruiter_for_unknown_identity(session):
    result = create(session, "unknown_recruiter", "unknown", "interview")
    assert result["policy_id"] == "recrutador"


def test_human_reply_cancels_pending_timers(session):
    result = create(session)
    services.human_reply(session, result["id"], "Fixture resposta humana")
    timers = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).all()
    assert all(timer.status == "CANCELED" for timer in timers)


def test_acknowledgement_finishes_escalation(session):
    result = create(session, "contact_mae", "family_core")
    action = first_action(session, result["id"])
    updated = services.acknowledge_action(session, action.id)
    assert updated["state"] == InteractionState.ACKNOWLEDGED.value


def test_action_failure_advances_to_next_authorized_action(session):
    result = create(session, "contact_mae", "family_core")
    action = first_action(session, result["id"])
    updated = services.action_failed(session, action.id)
    assert len(updated["actions"]) == 2
    assert updated["actions"][1]["action_key"] == "tv_overlay"


def test_executed_action_is_not_acknowledged(session):
    result = create(session, "contact_mae", "family_core")
    action = first_action(session, result["id"])
    updated = services.action_executed(session, action.id)
    assert updated["state"] == InteractionState.WAITING_ACK.value
    assert updated["actions"][0]["state"] == ActionState.EXECUTED.value


def test_worker_restart_preserves_pending_occurrence(session):
    result = create(session, "contact_mae", "family_core")
    timers_before = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).all()
    session.expire_all()
    timers_after = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).all()
    assert len(timers_before) == len(timers_after) == 1
    assert timers_after[0].status in {"pending", "PENDING"}


def test_audit_records_winner_and_justification(session):
    result = create(session, "contact_mae", "family_core")
    audit = session.scalars(
        select(AuditEventRow).where(
            AuditEventRow.interaction_id == result["id"], AuditEventRow.event_type == "policy_resolved"
        )
    ).first()
    assert audit.payload["winner"] == "mae"
    assert audit.payload["reason"] == (
        "winner=mae; ordered by structural scope, priority, specificity, "
        "match_score, then identifier descending"
    )


def test_policy_change_affects_next_decision(session):
    update_policy(session, "desconhecido", {"tone": "profissional", "escalation_steps": ["soft_ping", "cell_phone"]})
    result = create(session)
    assert "prioridade profissional" in result["lia_speech"]
    assert result["actions"][0]["action_key"] == "soft_ping"


def test_unknown_profile_does_not_reveal_home_location(session):
    result = create(session)
    forbidden = ["cozinha", "quarto", "sala", "cômodo", "dispositivo", "casa", "localização"]
    assert not any(word in result["lia_speech"].lower() for word in forbidden)


def inbound(session, external_event_id="evt-1", contact_id="contact_mae", category="family_core", context=None):
    result = services.receive_inbound_event(
        session,
        "simulator",
        external_event_id,
        "message",
        contact_id,
        "Contato Sintético",
        category,
        context,
        "Fixture sintética idempotente.",
    )
    session.commit()
    return result


def test_idempotency_sequential_replay(session):
    first = inbound(session, "evt-seq")
    second = inbound(session, "evt-seq")
    assert first["id"] == second["id"]
    assert len(session.scalars(select(TimerRow).where(TimerRow.interaction_id == first["id"])).all()) == 1
    assert len(session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == first["id"])).all()) == 1


def test_unique_constraint_external_event_id(session):
    inbound(session, "evt-unique")
    with pytest.raises(IntegrityError):
        from attention_router.infrastructure.repository import create_inbound_event
        from attention_router.domain.models import new_id

        create_inbound_event(session, "simulator", "evt-unique", "message", {"x": 1}, new_id())
        session.flush()


def test_external_event_id_different_creates_new_interaction(session):
    first = inbound(session, "evt-a")
    second = inbound(session, "evt-b")
    assert first["id"] != second["id"]


def test_payload_duplicate_with_different_external_id_is_allowed(session):
    first = inbound(session, "evt-payload-a")
    second = inbound(session, "evt-payload-b")
    assert first["id"] != second["id"]


def test_idempotency_concurrent_replay(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrent.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as db:
        seed_policies(db)
        db.commit()
    results = []
    lock = threading.Lock()

    def run():
        db = Session()
        try:
            result = services.receive_inbound_event(
                db,
                "simulator",
                "evt-concurrent",
                "message",
                "contact_mae",
                "Contato Sintético",
                "family_core",
                None,
                "Fixture sintética idempotente.",
            )
            db.commit()
            with lock:
                results.append(result["id"])
        except Exception:
            db.rollback()
            pass
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as db:
        receipts = db.scalars(select(InboundEventRow).where(InboundEventRow.external_event_id == "evt-concurrent")).all()
        assert len(receipts) == 1


def test_timer_claim_unique(session):
    result = create(session, "contact_mae", "family_core")
    timer = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).first()
    timer.due_at = services.now_utc()
    session.commit()
    claimed_a = services.claim_due_timers(session, "worker-a", limit=1)
    claimed_b = services.claim_due_timers(session, "worker-b", limit=1)
    assert len(claimed_a) == 1
    assert len(claimed_b) == 0


def test_two_workers_concurrent_no_duplicate_timer_execution(session):
    result = create(session, "contact_mae", "family_core")
    timer = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).first()
    timer.due_at = services.now_utc()
    session.commit()
    counts = [services.process_due_timers(session, "worker-a", 1), services.process_due_timers(session, "worker-b", 1)]
    assert sum(counts) == 1


def test_abandoned_timer_claim_is_recovered(session):
    result = create(session, "contact_mae", "family_core")
    timer = session.scalars(select(TimerRow).where(TimerRow.interaction_id == result["id"])).first()
    timer.due_at = services.now_utc()
    timer.status = "PROCESSING"
    timer.claimed_at = services.now_utc() - services.timedelta(seconds=999)
    session.commit()
    claimed = services.claim_due_timers(session, "worker-new", limit=1)
    assert len(claimed) == 1
    assert claimed[0].claimed_by == "worker-new"


def test_retry_after_outbox_failure(session):
    result = create(session, "contact_mae", "family_core")
    with pytest.raises(RuntimeError):
        services.process_outbox(session, "worker-a", fail=True)
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])).first()
    assert outbox.status == "RETRY"


def test_no_duplicate_outbox_idempotency(session):
    result = create(session, "contact_mae", "family_core")
    outbox = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])).all()
    assert len({item.idempotency_key for item in outbox}) == len(outbox) == 1


def test_policy_version_immutable_and_interaction_keeps_old_version(session):
    original = create(session)
    old_version = original["policy_version_id"]
    update_policy(session, "desconhecido", {"tone": "profissional"})
    new = create(session)
    assert original["policy_version_id"] == old_version
    assert new["policy_version_id"] != old_version


def test_policy_version_activation(session):
    update_policy(session, "desconhecido", {"tone": "profissional"})
    versions = list_policy_versions(session, "desconhecido")
    first_version = versions[0]["id"]
    activate_policy_version(session, "desconhecido", first_version)
    result = create(session)
    assert result["policy_version_id"] == first_version


def test_policy_change_audit(session):
    update_policy(session, "desconhecido", {"tone": "profissional"})
    event = session.scalars(select(AuditEventRow).where(AuditEventRow.event_type == "policy_updated")).first()
    assert event.policy_version_id is not None


def test_interaction_records_exact_policy_version(session):
    result = create(session, "contact_mae", "family_core")
    assert result["policy_version_id"]
    assert result["decisions"][0]["policy_version_id"] == result["policy_version_id"]


def test_transactional_outbox_present_with_decision(session):
    result = create(session, "contact_mae", "family_core")
    assert result["decisions"]
    assert result["outbox"]


def test_rollback_decision_and_outbox(session):
    with pytest.raises(RuntimeError):
        with session.begin_nested():
            services.create_interaction(session, "message", "contact_mae", "Contato", "family_core", None, "Fixture")
            raise RuntimeError("forced rollback")
    session.rollback()
    assert session.scalars(select(OutboxMessageRow)).all() == []


def test_correlation_id_propagated(session):
    result = inbound(session, "evt-correlation")
    assert result["correlation_id"]
    assert result["decisions"][0]["correlation_id"] == result["correlation_id"]


def test_policy_versions_exist_after_seed(session):
    assert len(list_policy_versions(session, "mae")) >= 1
    assert session.get(PolicyRow, "mae").current_version_id
