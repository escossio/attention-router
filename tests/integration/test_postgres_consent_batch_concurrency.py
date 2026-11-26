from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from test_postgres_context_preactivation import enroll_role  # noqa: F401
from test_postgres_source_consent import consent_clients, context_reader, lab  # noqa: F401

pytestmark = [pytest.mark.postgres, pytest.mark.parametrize("lab", ["client-query"], indirect=True)]


def test_concurrent_batch_readers_release_waiting_revoke(Session, consent_clients):  # noqa: F811
    args, writer, staged, decision, _ = consent_clients
    assert writer.grant(**args, **decision("proposal"))
    confirm = decision("confirm")
    assert writer.grant(**args, **confirm)
    entered = Event()
    release = Event()
    delivered = []

    def hold(body):
        delivered.append(body)
        entered.set()
        assert release.wait(5)

    def read(callback):
        return staged().query_and_deliver(**args, question="domingo", deliver=callback)

    def blocked_kind():
        with Session() as db:
            return set(db.execute(text("""
                SELECT CASE WHEN query ILIKE '%context_consent_scopes%' THEN 'consent'
                            WHEN query ILIKE '%actor_bindings%' THEN 'owner' END
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND wait_event_type = 'Lock'
                  AND cardinality(pg_blocking_pids(pid)) > 0
            """)).scalars())

    def observe(kind):
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if kind in blocked_kind():
                return
            sleep(0.02)
        pytest.fail(f"expected PostgreSQL lock wait: {kind}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(read, hold)
        try:
            assert entered.wait(4)
            second = pool.submit(read, delivered.append)
            observe("owner")
            revoke = pool.submit(writer.revoke, **args, message_id=confirm["message_id"])
            observe("consent")
            assert not first.done() and not second.done() and not revoke.done()
        finally:
            release.set()
        assert first.result(timeout=5).body_size > 0
        try:
            assert second.result(timeout=5).body_size > 0
        except ValueError as rejection:
            assert str(rejection) == "CONTEXT_CONSENT_REVISION_STALE"
        assert revoke.result(timeout=5)
    assert confirm["message_id"].encode() in delivered[0]
    after = []
    assert read(after.append).body_size > 0
    assert confirm["message_id"].encode() not in after[0]
    print("LOCK_WAIT=OWNER_AND_CONSENT; WRITER_PROGRESS=PASS; POST_REVOKE=DENIED")
