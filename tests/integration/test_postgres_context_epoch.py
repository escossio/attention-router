"""The synthetic authority persists independently of the disposable PG database."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from secrets import token_bytes
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from attention_router.application.context_epoch import ContextEpoch, sign_state
from attention_router.infrastructure.context_epoch_lab import LabEpochAuthority
from attention_router.infrastructure.models import AuditEventRow
import test_postgres_context_transactions as fixtures

boundary = fixtures.boundary
world = fixtures.world


pytestmark = pytest.mark.postgres


@pytest.fixture
def anchor(Session, world, tmp_path):
    key = token_bytes(32)
    authority = LabEpochAuthority(tmp_path / "authority.sqlite", authority_id="fixture-authority", key=key)

    def client():
        return ContextEpoch(authority, authority_id="fixture-authority", environment="isolated-ci", key=key)

    guard = client()
    with Session() as session:
        context = boundary(session)
        authority.initialize(guard.domain(context), guard.checkpoint(context))
    return guard, authority, key, client


def test_e01_e02_e11_e12_committed_checkpoint_reopens(Session, world, anchor):
    guard, _, _, client = anchor
    with Session.begin() as session:
        context = boundary(session)
        previous, packet = guard.compile(context, "fixture")
        assert previous.revision == 0 and packet.items
        guard.advance(context, context.revoke_message, world["ids"][0])
        future = guard.token(context)
        assert future.revision == previous.revision + 1
        assert future.generation.generation == previous.generation.generation + 1
    with Session() as restarted:
        context = boundary(restarted)
        assert client().token(context) == future
        with pytest.raises(ValueError, match="CONTEXT_EPOCH_TOKEN_STALE"):
            client().consume(context, previous, packet)


@pytest.mark.parametrize("keep_current_journal", [True, False], ids=["e03", "e04"])
def test_restore_recused_with_independent_anchor(Session, world, anchor, pg_url, keep_current_journal):
    guard, _, _, client = anchor
    engine = Session.kw["bind"]
    engine.dispose()
    origin = make_url(pg_url)
    prefix = "attention_router_test_"
    assert origin.database and origin.database.startswith(prefix)
    clone = prefix + uuid4().hex[:10]
    admin = create_engine(origin.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{clone}" TEMPLATE "{origin.database}"')
    restored = create_engine(origin.set(database=clone))
    Restored = sessionmaker(restored)
    try:
        with Session.begin() as session:
            context = boundary(session)
            guard.advance(context, context.revoke_message, world["ids"][0])
            event = session.scalar(
                select(AuditEventRow).where(AuditEventRow.event_type == "CONTEXT_VALIDITY_CHANGED")
            )
            event_data = {column.key: getattr(event, column.key) for column in AuditEventRow.__table__.columns}
        with Restored.begin() as session:
            if keep_current_journal:
                session.add(AuditEventRow(**event_data))
        with Restored() as session:
            with pytest.raises(ValueError, match="CONTEXT_EPOCH_(JOURNAL_INVALID|RECONCILIATION_REQUIRED)"):
                client().compile(boundary(session), "fixture")
    finally:
        restored.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{clone}"')
        admin.dispose()


def test_e05_missing_authority_denies(Session, anchor):
    guard, authority, _, _ = anchor
    authority.path.unlink()
    with Session() as session:
        with pytest.raises(ValueError, match="CONTEXT_EPOCH_AUTHORITY_UNAVAILABLE"):
            guard.compile(boundary(session), "fixture")


@pytest.mark.parametrize("failure", ["old", "tampered", "other_domain", "other_authority"])
def test_e06_e07_e08_untrusted_response_denies(Session, world, anchor, monkeypatch, failure):
    guard, authority, key, _ = anchor
    with Session.begin() as session:
        context = boundary(session)
        previous, _ = authority.read(guard.domain(context), "initial")
        guard.advance(context, context.revoke_message, world["ids"][0])
        guard.token(context)
    with Session() as session:
        context = boundary(session)
        current, _ = authority.read(guard.domain(context), "current")
        states = {
            "old": previous,
            "tampered": current,
            "other_domain": replace(current, domain="different-fixture-scope"),
            "other_authority": replace(current, authority="different-fixture-authority"),
        }
        state = states[failure]

        def read(domain, challenge):
            return state, sign_state(key, state, "previous-challenge" if failure == "tampered" else challenge)

        monkeypatch.setattr(authority, "read", read)
        with pytest.raises(ValueError, match="CONTEXT_EPOCH_(PROOF_INVALID|AUTHORITY_REGRESSED)"):
            guard.compile(context, "fixture")


def test_e09_compare_and_set_has_one_winner(Session, anchor):
    guard, authority, _, _ = anchor
    with Session() as session:
        state, _ = authority.read(guard.domain(boundary(session)), "cas")
    ready = Barrier(2)

    def advance(checkpoint):
        ready.wait(timeout=5)
        try:
            authority.advance(state, replace(state, revision=state.revision + 1, checkpoint=checkpoint))
            return "advanced"
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(advance, ("fixture-a", "fixture-b"))) == [
            "CONTEXT_EPOCH_CAS_CONFLICT", "advanced",
        ]


def test_e10_interruption_after_cas_blocks_old_database(Session, world, anchor):
    guard, _, _, client = anchor
    with Session() as interrupted:
        context = boundary(interrupted)
        previous = guard.token(context)
        guard.advance(context, context.revoke_message, world["ids"][0])
        interrupted.rollback()
    with Session() as session:
        context = boundary(session)
        assert context.token() == previous.generation
        with pytest.raises(ValueError, match="CONTEXT_EPOCH_RECONCILIATION_REQUIRED"):
            client().compile(context, "fixture")
