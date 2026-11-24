"""Real row-lock races, stale sessions and backend termination on disposable PG."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import select, text

from attention_router.application.context_validity import ContextValidity
from attention_router.application.candidate_insights import candidate_insight_source_is_current
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.models import ConversationMessageRow, AuditEventRow
from test_cognitive_observation import _exercise, _attachment, START, ACCOUNT

pytestmark = pytest.mark.postgres


@pytest.fixture
def world(Session, monkeypatch):
    result = {}
    with Session() as session:
        def save(db, observe, ids, registered):
            result["ids"] = ids
            result["candidate"] = db.scalar(select(CandidateInsightRow.id))
            db.commit()
        _exercise(session, monkeypatch, prior_to_final_withdrawal=save)
    return result


def boundary(session):
    return ContextValidity(
        session, tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
        owner_human_identity_id="fixture-human-owner", source_account=ACCOUNT,
        start=START, now=START + timedelta(days=4),
    )


def operation(validity, kind, world):
    if kind == "observe":
        return validity.observe(world["ids"])
    if kind == "admit":
        return validity.admit(world["candidate"], "fixture:owner-admission")
    if kind == "graph":
        return validity.graph()
    if kind == "partial":
        return validity.revoke_message(world["ids"][0])
    if kind == "new":
        mid, _, _ = _attachment(validity.session, "new-evidence", 3)
        validity.session.flush()
        return validity.observe((*world["ids"], mid))
    for mid in world["ids"]:
        validity.revoke_message(mid)
    return validity.token()


@pytest.mark.parametrize("first,second", [
    ("observe", "revoke"), ("revoke", "observe"),
    ("admit", "revoke"), ("revoke", "admit"),
    ("revoke", "observe"), ("graph", "revoke"),
    ("partial", "new"), ("new", "partial"),
])
def test_locked_races(Session, world, first, second):
    ready = Event()
    pids = {}
    errors = []
    with Session() as holder:
        holder.execute(text("SET LOCAL lock_timeout='8s'"))
        pids["holder"] = holder.scalar(text("select pg_backend_pid()"))
        valid = boundary(holder)
        operation(valid, first, world)

        def compete():
            with Session() as other:
                other.execute(text("SET LOCAL lock_timeout='8s'"))
                pids["other"] = other.scalar(text("select pg_backend_pid()"))
                ready.set()
                try:
                    operation(boundary(other), second, world)
                    other.commit()
                except Exception as error:
                    other.rollback()
                    errors.append(error)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(compete)
            assert ready.wait(5)
            assert pids["holder"] != pids["other"]
            deadline = monotonic() + 5
            with holder.get_bind().connect() as inspector:
                while monotonic() < deadline:
                    blocking = inspector.scalar(
                        text("select :holder = ANY(pg_blocking_pids(:other))"),
                        pids,
                    )
                    if blocking:
                        break
                else:
                    pytest.fail("Contender never demonstrably blocked on holder")
            holder.commit()
            future.result(timeout=10)
    if first == "revoke" and second == "admit":
        assert len(errors) == 1
        assert "EVIDENCE_REVOKED" in str(errors[0])
    else:
        assert not errors
    with Session() as check:
        current = boundary(check)
        rows = check.scalars(select(CandidateInsightRow)).all()
        eligible = [r for r in rows if candidate_insight_source_is_current(check, r, now=START + timedelta(days=4))]
        if "partial" not in (first, second):
            assert not eligible
            assert current.observe(world["ids"]).new_candidates == 0
        else:
            # Converge under the same lock after both orderings.
            ids = tuple(check.scalars(select(ConversationMessageRow.id).where(
                ConversationMessageRow.source_account == ACCOUNT,
                ConversationMessageRow.searchable.is_(True),
            )))
            current.observe(ids)
            refreshed = check.scalars(select(CandidateInsightRow)).all()
            remaining = [r for r in refreshed if candidate_insight_source_is_current(
                check, r, now=START + timedelta(days=4),
            )]
            assert remaining
            assert all(r.state == "PROPOSED" and r.decision_ref is None for r in remaining)
            assert not check.get(ConversationMessageRow, world["ids"][0]).searchable
        if first == "admit":
            original = check.get(CandidateInsightRow, world["candidate"])
            assert original.state == "ADMITTED"
            assert original.decision_ref == "fixture:owner-admission"
            assert original.evidence_status == "EVIDENCE_REVOKED"


def test_stale_identity_map_and_snapshot(Session, world):
    with Session() as stale:
        old = stale.get(ConversationMessageRow, world["ids"][0])
        token = boundary(stale).token()
        stale.commit()
        with Session() as writer:
            boundary(writer).revoke_message(old.id)
            writer.commit()
        assert old.searchable  # Demonstrate stale ORM state before boundary.
        fresh = boundary(stale)
        assert not old.searchable
        with pytest.raises(ValueError, match="STALE_GENERATION"):
            fresh.validate(token)
        fresh.revoke_message(old.id)
        assert fresh.token().generation == 1  # repeated revocation is idempotent
        stale.commit()
        with pytest.raises(ValueError, match="TRANSACTION_ENDED"):
            fresh.token()


def test_backend_termination_rolls_back_and_recovers(Session, world):
    with Session() as victim:
        pid = victim.scalar(text("select pg_backend_pid()"))
        validity = boundary(victim)
        validity.revoke_message(world["ids"][0])
        assert validity.token().generation == 1
        with victim.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as control:
            assert control.scalar(text("select pg_backend_pid()")) != pid
            assert control.scalar(text("select pg_terminate_backend(:pid)"), dict(pid=pid))
        with pytest.raises(Exception):
            victim.commit()
        victim.rollback()
    with Session() as recovered:
        validity = boundary(recovered)
        assert validity.token().generation == 0
        assert recovered.get(ConversationMessageRow, world["ids"][0]).searchable
        assert recovered.scalar(select(AuditEventRow.id).where(
            AuditEventRow.event_type == "CONTEXT_VALIDITY_CHANGED",
        )) is None
        validity.revoke_message(world["ids"][0])
        recovered.commit()
    with Session() as after_commit:
        validity = boundary(after_commit)
        assert validity.token().generation == 1
        validity.revoke_message(world["ids"][0])
        assert validity.token().generation == 1


@pytest.mark.parametrize("kind", ["episode", "candidate"])
def test_downgrade_refuses_revoked_data(Session, world, pg_url, kind):
    import os
    import subprocess
    import sys
    from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeRow

    model = SemanticEpisodeRow if kind == "episode" else CandidateInsightRow
    with Session() as session:
        row = session.scalar(select(model))
        row.evidence_status = "EVIDENCE_REVOKED"
        session.commit()
    env = dict(os.environ, DATABASE_URL=pg_url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0068_google_contacts_cache"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "DOWNGRADE_REQUIRES_NO_REVOKED_ROWS" in result.stderr
    with Session() as session:
        from tests.integration.migration_head import current_schema_head
        assert session.scalar(text("select version_num from alembic_version")) == current_schema_head()
        assert session.scalar(select(model)).evidence_status == "EVIDENCE_REVOKED"


def test_context_schema_roundtrip_without_pilot_history(Session, pg_url):
    import os
    import subprocess
    import sys

    from sqlalchemy import create_engine, inspect
    from tests.integration.migration_head import current_schema_head

    baseline = "0068_google_contacts_cache"
    head = current_schema_head()
    engine = create_engine(pg_url, hide_parameters=True)
    env = dict(os.environ, DATABASE_URL=pg_url)

    def run_migration(action, revision):
        subprocess.run([sys.executable, "-m", "alembic", action, revision],
                       env=env, check=True, capture_output=True, text=True, timeout=30)

    def assert_schema(revision, *, contextual):
        with engine.connect() as connection:
            assert connection.scalar(text("select version_num from alembic_version")) == revision
            columns = inspect(connection)
            assert ("context_revision" in {
                column["name"] for column in columns.get_columns("client_tenant_memberships")
            }) is contextual
            assert ("evidence_status" in {
                column["name"] for column in columns.get_columns("candidate_insights")
            }) is contextual
            assert connection.scalar(text(
                "select count(*) from pg_trigger where tgname = 'context_receipt_tombstone'"
            )) == int(contextual)

    try:
        try:
            run_migration("downgrade", baseline)
            assert_schema(baseline, contextual=False)
        finally:
            run_migration("upgrade", head)
        assert_schema(head, contextual=True)
    finally:
        engine.dispose()
