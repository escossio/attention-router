import threading
import uuid

import pytest
from sqlalchemy import select, text

from attention_router.application import services
from attention_router.infrastructure.models import (
    DecisionRow,
    InboundEventRow,
    OutboxMessageRow,
    PolicyVersionRow,
    TimerRow,
)
from attention_router.infrastructure.repository import update_policy


pytestmark = pytest.mark.postgres


def create_event(session, event_id: str, contact="contact_mae"):
    return services.receive_inbound_event(
        session,
        "pgtest",
        event_id,
        "message",
        contact,
        "Contato Sintético",
        "family_core" if contact == "contact_mae" else "unknown",
        None,
        "Fixture PostgreSQL.",
    )


def test_pg_concurrent_deduplication(Session):
    event_id = f"evt-{uuid.uuid4()}"
    results = []

    def run():
        with Session() as session:
            try:
                result = create_event(session, event_id)
                session.commit()
                results.append(result["id"])
            except Exception:
                session.rollback()
                raise

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as session:
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == event_id)) == 1
        assert len(set(results)) == 1


def test_pg_skip_locked_timers(Session):
    with Session() as session:
        ids = [create_event(session, f"timer-{uuid.uuid4()}")["id"] for _ in range(3)]
        session.execute(update_due_sql(ids))
        session.commit()
    counts = []

    def worker(name):
        with Session() as session:
            counts.append(services.process_due_timers(session, name, 10))
            session.commit()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as session:
        assert sum(counts) >= 3
        assert session.scalar(select(text("count(*)")).select_from(TimerRow).where(TimerRow.status == "DONE")) >= 3


def update_due_sql(ids):
    quoted = ",".join(f"'{item}'" for item in ids)
    return text(f"update timers set due_at=now(), status='PENDING', claimed_at=null where interaction_id in ({quoted})")


def test_pg_skip_locked_outbox(Session):
    with Session() as session:
        ids = [create_event(session, f"outbox-{uuid.uuid4()}")["id"] for _ in range(3)]
        session.execute(text(f"update outbox_messages set status='PENDING', available_at=now() where interaction_id in ({','.join(repr(i) for i in ids)})"))
        session.commit()
    counts = []

    def worker(name):
        with Session() as session:
            counts.append(services.process_outbox(session, name, 10))
            session.commit()

    threads = [threading.Thread(target=worker, args=(f"ow{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as session:
        assert sum(counts) >= 3
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow).where(OutboxMessageRow.status == "DONE")) >= 3


def test_pg_unique_constraint_real(Session):
    with Session() as session:
        create_event(session, "unique-real")
        session.commit()
    with Session() as session:
        result = create_event(session, "unique-real")
        session.commit()
        assert session.scalar(select(text("count(*)")).select_from(InboundEventRow).where(InboundEventRow.external_event_id == "unique-real")) == 1
        assert result["id"]


def test_pg_transactional_outbox_rollback(Session):
    with Session() as session:
        before_o = session.scalar(select(text("count(*)")).select_from(OutboxMessageRow))
        before_d = session.scalar(select(text("count(*)")).select_from(DecisionRow))
        try:
            create_event(session, f"rollback-{uuid.uuid4()}")
            raise RuntimeError("forced")
        except RuntimeError:
            session.rollback()
        assert session.scalar(select(text("count(*)")).select_from(OutboxMessageRow)) == before_o
        assert session.scalar(select(text("count(*)")).select_from(DecisionRow)) == before_d


def test_pg_policy_version_history(Session):
    with Session() as session:
        old = create_event(session, f"pv-old-{uuid.uuid4()}", "unknown_contact")
        update_policy(session, "desconhecido", {"tone": "profissional"})
        new = create_event(session, f"pv-new-{uuid.uuid4()}", "unknown_contact")
        session.commit()
        assert old["policy_version_id"] != new["policy_version_id"]
        assert session.get(PolicyVersionRow, old["policy_version_id"])


def test_pg_claim_recovery(Session):
    with Session() as session:
        result = create_event(session, f"recovery-{uuid.uuid4()}")
        session.execute(text("update timers set status='PROCESSING', claimed_at=now() - interval '10 minutes', due_at=now() where interaction_id=:id"), {"id": result["id"]})
        session.commit()
    with Session() as session:
        claimed = services.claim_due_timers(session, "recoverer", 1)
        session.commit()
        assert len(claimed) == 1


def test_pg_retry_outbox(Session):
    with Session() as session:
        create_event(session, f"retry-{uuid.uuid4()}")
        session.commit()
    with Session() as session:
        with pytest.raises(RuntimeError):
            services.process_outbox(session, "retry-worker", fail=True)
        row = session.scalars(select(OutboxMessageRow).where(OutboxMessageRow.status == "RETRY")).first()
        assert row.attempt_count == 1
        assert row.last_error
        outbox_id = row.id
        session.commit()
    with Session() as session:
        assert services.process_outbox(session, "retry-worker-ok") == 1
        row = session.get(OutboxMessageRow, outbox_id)
        assert row.status == "DONE"
        assert row.attempt_count == 2
        assert row.completed_at is not None
