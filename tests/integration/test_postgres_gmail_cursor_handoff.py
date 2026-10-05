"""PostgreSQL serialization proof for Gmail cross-tenant cursor handoff."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from attention_router.application.gmail_cursor_handoff import (
    handoff_gmail_history_cursor,
)
from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)
from test_gmail_cursor_handoff import (
    DESTINATION,
    NOW,
    SOURCE,
    _seed,
)


pytestmark = pytest.mark.postgres


def _wait_for_block(Session, pid: int, blocker: int) -> None:
    deadline = monotonic() + 5
    with Session() as observer:
        while monotonic() < deadline:
            blockers = observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"),
                {"pid": pid},
            )
            if blocker in blockers:
                return
            sleep(0.01)
    pytest.fail("cursor handoff did not wait on the expected Gmail slot")


@pytest.mark.parametrize("held", ["source", "destination"])
def test_handoff_serializes_on_both_gmail_slots(Session, held):
    with Session() as seed:
        source, destination, *_ = _seed(seed)
        source_slot = source.slot_key
        destination_slot = destination.slot_key

    pids = Queue()

    def apply_handoff():
        with Session() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            pids.put(session.scalar(text("SELECT pg_backend_pid()")))
            result = handoff_gmail_history_cursor(
                session,
                source_installation_id=SOURCE,
                destination_installation_id=DESTINATION,
                apply=True,
                now=NOW,
            )
            session.commit()
            return result.state

    slot = source_slot if held == "source" else destination_slot
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session() as blocker:
            blocker_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
            assert acquire_gmail_slot(blocker, slot)
            future = pool.submit(apply_handoff)
            waiter_pid = pids.get(timeout=5)
            try:
                _wait_for_block(Session, waiter_pid, blocker_pid)
                with Session() as observer:
                    assert (
                        observer.get(
                            ProviderAuthorizationRow,
                            DESTINATION,
                        ).gmail_history_id
                        is None
                    )
            finally:
                blocker.rollback()
        assert future.result(timeout=12) == "APPLIED"

    with Session() as check:
        assert (
            check.get(
                ProviderAuthorizationRow,
                DESTINATION,
            ).gmail_history_id
            == "123456"
        )


def test_concurrent_replay_is_applied_once_then_idempotent(Session):
    with Session() as seed:
        _seed(seed)

    def apply_handoff():
        with Session() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            result = handoff_gmail_history_cursor(
                session,
                source_installation_id=SOURCE,
                destination_installation_id=DESTINATION,
                apply=True,
                now=NOW,
            )
            session.commit()
            return result.state

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = sorted(
            future.result(timeout=12)
            for future in (
                pool.submit(apply_handoff),
                pool.submit(apply_handoff),
            )
        )

    assert states == ["ALREADY_APPLIED", "APPLIED"]
    with Session() as check:
        assert (
            check.get(
                ProviderAuthorizationRow,
                DESTINATION,
            ).gmail_history_id
            == "123456"
        )
