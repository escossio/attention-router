"""Actual Gmail mutation contention while independent neutral ingress admits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from attention_router.application.gmail_connection import GmailConnectionService, GoogleGmailProfile
from attention_router.application.gmail_history import GmailProductHistoryBusy
from attention_router.application.gmail_product_runner import GmailProductRunner
from attention_router.config import settings
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.admission import admit_inbound
from attention_router.integrations.gmail_connector import IntegrationIngressResponse
from test_gmail_history import Reader
from test_gmail_product_runner import (
    FakeClientSessions, FakeOAuth, FakeRefresher, SESSION_TOKEN, _enabled, _seed_connected,
)

pytestmark = pytest.mark.postgres


def wait_for_block(Session, pid, blocker):
    deadline = monotonic() + 5
    with Session() as observer:
        while monotonic() < deadline:
            blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': pid})
            if blocker in blockers:
                return
            sleep(0.01)
    pytest.fail('Mutation did not contend with runner')


@pytest.mark.parametrize('mutation', ['reconnect', 'replace', 'disconnect'])
def test_runner_admits_while_mutation_waits(Session, monkeypatch, mutation):
    stamp = datetime.now(UTC)
    with Session() as seed:
        installation = _seed_connected(seed, monkeypatch, now=stamp)
        seed.get(ProviderAuthorizationRow, installation).gmail_history_id = '10'
        seed.commit()
    _enabled(monkeypatch)
    pids = Queue()
    oauth = FakeOAuth()
    oauth.revoke = lambda token: None
    if mutation == 'replace':
        oauth.gmail_profile = lambda token: GoogleGmailProfile('replacement@example.invalid')
    service = GmailConnectionService(settings=settings, client_sessions=FakeClientSessions(), oauth=oauth)

    def mutate():
        with Session() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            pids.put(session.scalar(text('SELECT pg_backend_pid()')))
            if mutation == 'disconnect':
                service.disconnect(session, session_token=SESSION_TOKEN, now=stamp)
            else:
                service.connect(session, session_token=SESSION_TOKEN,
                                authorization_code='server-auth-code', now=stamp)
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session() as running:
            runner_pid = running.scalar(text('SELECT pg_backend_pid()'))
            futures = []

            class Ingress:
                def __init__(self, bearer):
                    self.bearer = bearer

                def send(self, payload):
                    futures.append(pool.submit(mutate))
                    wait_for_block(Session, pids.get(timeout=5), runner_pid)
                    # A second runner must fail promptly even with a queued mutation.
                    with Session() as contender:
                        started = monotonic()
                        with pytest.raises(GmailProductHistoryBusy):
                            runner.run_incremental(contender, installation_id=installation, now=stamp)
                        assert monotonic() - started < 1
                    receipt = admit_inbound(Session, self.bearer, json.dumps(payload).encode(),
                                            audience=settings.integration_ingress_audience)
                    assert receipt.code == 'ACCEPTED'
                    assert not futures[0].done()
                    return IntegrationIngressResponse(202, {'status': 'accepted'})

            runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher(),
                                        reader_factory=lambda token: Reader(), ingress_factory=Ingress)
            try:
                result = runner.run_incremental(running, installation_id=installation, now=stamp)
                assert result.accepted == 1
                running.commit()
            finally:
                running.rollback()
        for future in futures:
            future.result(timeout=12)
    with Session() as check:
        row = check.get(ProviderAuthorizationRow, installation)
        assert row.gmail_history_id == (None if mutation == 'replace' else '20')
        assert row.status == ('REVOKED' if mutation == 'disconnect' else 'ACTIVE')


def test_first_connect_and_rollback_release_same_slot(Session, monkeypatch):
    """The gate exists before its installation row and survives method return."""
    stamp = datetime.now(UTC)
    with Session() as seed:
        installation = _seed_connected(seed, monkeypatch, now=stamp)
        seed.delete(seed.get(ProviderAuthorizationRow, installation))
        seed.commit()
    _enabled(monkeypatch)
    service = GmailConnectionService(settings=settings, client_sessions=FakeClientSessions(),
                                     oauth=FakeOAuth())
    pids = Queue()

    def connect():
        with Session() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            pids.put(session.scalar(text('SELECT pg_backend_pid()')))
            result = service.connect(session, session_token=SESSION_TOKEN,
                                     authorization_code='server-auth-code', now=stamp)
            session.commit()
            return result.installation_id

    from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
    from test_gmail_product_runner import HUMAN, TENANT
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session() as first:
            pid = first.scalar(text('SELECT pg_backend_pid()'))
            # Same gate as connect, before any installation exists.
            assert acquire_gmail_slot(first, service._slot_key(TENANT, HUMAN))
            future = pool.submit(connect)
            try:
                waiter = pids.get(timeout=5)
                wait_for_block(Session, waiter, pid)
                with Session() as observer:
                    assert observer.scalar(text(
                        "SELECT count(*) FROM pg_locks WHERE pid=:pid "
                        "AND locktype='advisory' AND NOT granted"
                    ), {'pid': waiter}) == 1
                    # Waiting first connect must not own Tenant either.
                    observer.execute(text('SELECT id FROM tenants FOR UPDATE NOWAIT'))
            finally:
                first.rollback()
        new_installation = future.result(timeout=12)
    runner = GmailProductRunner(settings=settings, token_refresher=FakeRefresher(),
                                reader_factory=lambda token: Reader())
    with Session() as first, Session() as second:
        assert runner.run_incremental(first, installation_id=new_installation, now=stamp).initialized
        with pytest.raises(GmailProductHistoryBusy):
            runner.run_incremental(second, installation_id=new_installation, now=stamp)
        second.rollback()
        first.rollback()
        # Rollback releases both the gate and baseline, with no hidden commit.
        assert runner.run_incremental(second, installation_id=new_installation, now=stamp).initialized
        second.commit()
