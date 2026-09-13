"""Repository behavior against the disposable, Alembic-migrated PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
import threading

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from attention_router.core.human_identity import HumanAuthChallengeConsumed
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanIdentityRow,
)
from attention_router.infrastructure.human_identity_repository import (
    create_human_auth_transaction,
    get_human_auth_transaction,
    lock_human_auth_transaction,
    mark_human_auth_rejected,
    mark_human_auth_verified,
    resolve_human_identity,
)


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 13, tzinfo=UTC)
CHALLENGE_ID = "hac_repositorychallenge123456789"


@pytest.fixture
def repository_sessions():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=15000 -c lock_timeout=10000"},
    )
    reset = text(
        "TRUNCATE TABLE human_auth_transactions, external_identity_bindings, human_identities"
    )
    try:
        with engine.begin() as connection:
            connection.execute(reset)
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        with engine.begin() as connection:
            connection.execute(reset)
        engine.dispose()


@pytest.fixture
def pending_transaction_id(repository_sessions):
    with repository_sessions.begin() as session:
        create_human_auth_transaction(
            session, transaction_id=CHALLENGE_ID, provider="google", nonce_digest="a" * 64,
            now=NOW, expires_at=NOW + timedelta(seconds=300),
        )
    return CHALLENGE_ID


def _values(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def test_create_transaction_leaves_commit_and_rollback_to_caller(repository_sessions):
    with repository_sessions() as session:
        row = create_human_auth_transaction(
            session, transaction_id=CHALLENGE_ID, provider="google", nonce_digest="a" * 64,
            now=NOW, expires_at=NOW + timedelta(seconds=300),
        )
        assert _values(row) == {
            "id": CHALLENGE_ID, "provider": "google", "nonce_digest": "a" * 64,
            "state": "PENDING", "created_at": NOW,
            "expires_at": NOW + timedelta(seconds=300),
            "consumed_at": None, "resolved_human_identity_id": None,
        }
        session.flush()
        assert session.get(HumanAuthTransactionRow, CHALLENGE_ID) is row
        with repository_sessions() as observer:
            assert observer.get(HumanAuthTransactionRow, CHALLENGE_ID) is None
        session.rollback()
    with repository_sessions() as observer:
        assert observer.get(HumanAuthTransactionRow, CHALLENGE_ID) is None


def test_get_transaction_and_missing_id(repository_sessions, pending_transaction_id):
    with repository_sessions() as session:
        row = get_human_auth_transaction(session, pending_transaction_id)
        assert row.id == pending_transaction_id
        assert row.state == "PENDING"
        assert get_human_auth_transaction(session, "hac_missing") is None


def test_lock_transaction_excludes_another_postgres_lock(
    repository_sessions, pending_transaction_id,
):
    with repository_sessions.begin() as owner:
        row = lock_human_auth_transaction(owner, pending_transaction_id)
        assert row.id == pending_transaction_id
        assert lock_human_auth_transaction(owner, "hac_missing") is None
        with pytest.raises(OperationalError) as error:
            with repository_sessions.begin() as contender:
                contender.execute(
                    select(HumanAuthTransactionRow).where(
                        HumanAuthTransactionRow.id == pending_transaction_id
                    ).with_for_update(nowait=True)
                )
        assert error.value.orig.sqlstate == "55P03"
    with repository_sessions.begin() as contender:
        assert lock_human_auth_transaction(contender, pending_transaction_id).id == (
            pending_transaction_id
        )


def test_lock_refreshes_state_loaded_before_another_commit(
    repository_sessions, pending_transaction_id,
):
    with repository_sessions.begin() as reader:
        row = get_human_auth_transaction(reader, pending_transaction_id)
        assert row.state == "PENDING"
        with repository_sessions.begin() as writer:
            mark_human_auth_rejected(
                get_human_auth_transaction(writer, pending_transaction_id), now=NOW,
            )
        locked = lock_human_auth_transaction(reader, pending_transaction_id)
        assert locked is row
        assert locked.state == "REJECTED"
        assert locked.consumed_at == NOW


def test_stable_provider_subject_resolves_one_identity(repository_sessions):
    with repository_sessions.begin() as session:
        first = resolve_human_identity(session, provider="google", subject="stable-sub", now=NOW)
        second = resolve_human_identity(session, provider="google", subject="stable-sub", now=NOW)
        assert first == second
        assert first.startswith("hid_")
    with repository_sessions() as session:
        bindings = session.scalars(select(ExternalIdentityBindingRow)).all()
        assert len(bindings) == 1
        assert (bindings[0].provider, bindings[0].subject) == ("google", "stable-sub")
        assert bindings[0].human_identity_id == first
        assert session.scalar(select(func.count()).select_from(HumanIdentityRow)) == 1


def test_different_subjects_resolve_different_identities(repository_sessions):
    with repository_sessions.begin() as session:
        first = resolve_human_identity(session, provider="google", subject="first-sub", now=NOW)
        second = resolve_human_identity(session, provider="google", subject="second-sub", now=NOW)
        assert first != second
        assert first.startswith("hid_") and second.startswith("hid_")


def test_provider_is_part_of_binding_key(repository_sessions):
    with repository_sessions.begin() as session:
        first = resolve_human_identity(session, provider="google", subject="same-sub", now=NOW)
        second = resolve_human_identity(session, provider="synthetic", subject="same-sub", now=NOW)
        assert first != second


def test_existing_binding_changes_only_last_verified_at(repository_sessions):
    with repository_sessions.begin() as session:
        identity_id = resolve_human_identity(session, provider="google", subject="stable-sub", now=NOW)
        binding = session.scalars(select(ExternalIdentityBindingRow)).one()
        before = _values(binding)
        identity_before = _values(session.get(HumanIdentityRow, identity_id))
    later = NOW + timedelta(minutes=10)
    with repository_sessions.begin() as session:
        assert resolve_human_identity(
            session, provider="google", subject="stable-sub", now=later,
        ) == identity_id
    with repository_sessions() as session:
        binding = session.scalars(select(ExternalIdentityBindingRow)).one()
        assert _values(binding) == {**before, "last_verified_at": later}
        assert _values(session.get(HumanIdentityRow, identity_id)) == identity_before
        assert session.scalar(select(func.count()).select_from(HumanIdentityRow)) == 1


def test_resolve_savepoint_does_not_commit_the_outer_transaction(repository_sessions):
    with repository_sessions() as session:
        identity_id = resolve_human_identity(session, provider="google", subject="rollback-sub", now=NOW)
        with repository_sessions() as observer:
            assert observer.get(HumanIdentityRow, identity_id) is None
        session.rollback()
    with repository_sessions() as observer:
        assert observer.scalar(select(func.count()).select_from(HumanIdentityRow)) == 0
        assert observer.scalar(select(func.count()).select_from(ExternalIdentityBindingRow)) == 0


def test_mark_verified_persists_resolution(repository_sessions, pending_transaction_id):
    with repository_sessions.begin() as session:
        identity_id = resolve_human_identity(session, provider="google", subject="verified-sub", now=NOW)
        row = get_human_auth_transaction(session, pending_transaction_id)
        mark_human_auth_verified(row, human_identity_id=identity_id, now=NOW)
        assert row.state == "VERIFIED"
        assert row.consumed_at == NOW
        assert row.resolved_human_identity_id == identity_id
    with repository_sessions() as session:
        row = get_human_auth_transaction(session, pending_transaction_id)
        assert (row.state, row.consumed_at, row.resolved_human_identity_id) == (
            "VERIFIED", NOW, identity_id,
        )


def test_mark_rejected_clears_resolution(repository_sessions, pending_transaction_id):
    with repository_sessions.begin() as session:
        row = get_human_auth_transaction(session, pending_transaction_id)
        row.resolved_human_identity_id = resolve_human_identity(
            session, provider="google", subject="rejected-sub", now=NOW,
        )
        mark_human_auth_rejected(row, now=NOW)
        assert (row.state, row.consumed_at, row.resolved_human_identity_id) == (
            "REJECTED", NOW, None,
        )
    with repository_sessions() as session:
        row = get_human_auth_transaction(session, pending_transaction_id)
        assert (row.state, row.consumed_at, row.resolved_human_identity_id) == (
            "REJECTED", NOW, None,
        )


@pytest.mark.parametrize("state", ["VERIFIED", "REJECTED"])
@pytest.mark.parametrize("action", ["verify", "reject"])
def test_consumed_transaction_cannot_mutate_again(
    repository_sessions, pending_transaction_id, state, action,
):
    with repository_sessions.begin() as session:
        row = get_human_auth_transaction(session, pending_transaction_id)
        identity_id = resolve_human_identity(session, provider="google", subject="consumed-sub", now=NOW)
        if state == "VERIFIED":
            mark_human_auth_verified(row, human_identity_id=identity_id, now=NOW)
        else:
            mark_human_auth_rejected(row, now=NOW)
        before = _values(row)
        with pytest.raises(HumanAuthChallengeConsumed):
            if action == "verify":
                mark_human_auth_verified(
                    row, human_identity_id=identity_id, now=NOW + timedelta(seconds=1),
                )
            else:
                mark_human_auth_rejected(row, now=NOW + timedelta(seconds=1))
        assert _values(row) == before


def test_unrelated_integrity_error_is_propagated_without_outer_rollback(repository_sessions):
    with repository_sessions.begin() as session:
        create_human_auth_transaction(
            session, transaction_id=CHALLENGE_ID, provider="google", nonce_digest="a" * 64,
            now=NOW, expires_at=NOW + timedelta(seconds=300),
        )
        with pytest.raises(IntegrityError) as error:
            resolve_human_identity(session, provider=None, subject="invalid-provider", now=NOW)
        assert error.value.orig.sqlstate == "23502"
        assert error.value.orig.diag.column_name == "provider"
        assert session.is_active
        assert get_human_auth_transaction(session, CHALLENGE_ID) is not None
    with repository_sessions() as session:
        assert get_human_auth_transaction(session, CHALLENGE_ID) is not None
        assert session.scalar(select(func.count()).select_from(HumanIdentityRow)) == 0


def test_concurrent_first_login_preserves_outer_work_without_orphans(repository_sessions):
    engine = repository_sessions.kw["bind"]
    start = threading.Barrier(2)
    missing_binding_reads = threading.Barrier(2)
    unique_conflicts = []

    def synchronize_missing_reads(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "FROM external_identity_bindings" in statement
            and connection.info.pop("synchronize_identity_lookup", False)
        ):
            # Both real SELECTs finish before either worker can create a binding.
            missing_binding_reads.wait(timeout=10)

    def observe_constraint_error(context):
        diagnostic = getattr(context.original_exception, "diag", None)
        unique_conflicts.append(getattr(diagnostic, "constraint_name", None))

    def worker(index):
        with repository_sessions.begin() as session:
            row = create_human_auth_transaction(
                session, transaction_id=f"hac_race_{index}", provider="google",
                nonce_digest=str(index) * 64, now=NOW,
                expires_at=NOW + timedelta(seconds=300),
            )
            session.connection().info["synchronize_identity_lookup"] = True
            start.wait(timeout=10)
            result = resolve_human_identity(session, provider="google", subject="same-sub", now=NOW)
            mark_human_auth_verified(row, human_identity_id=result, now=NOW)
        # Release the unique-key conflict by committing before collecting either Future.
        return result

    event.listen(engine, "after_cursor_execute", synchronize_missing_reads)
    event.listen(engine, "handle_error", observe_constraint_error)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, index) for index in (1, 2)]
            results = [future.result(timeout=30) for future in futures]
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_missing_reads)
        event.remove(engine, "handle_error", observe_constraint_error)

    assert results[0] == results[1]
    assert results[0].startswith("hid_")
    assert unique_conflicts == ["uq_external_identity_provider_subject"]
    with repository_sessions() as session:
        binding = session.scalars(select(ExternalIdentityBindingRow).where(
            ExternalIdentityBindingRow.provider == "google",
            ExternalIdentityBindingRow.subject == "same-sub",
        )).one()
        assert binding.human_identity_id == results[0]
        assert session.scalar(select(func.count()).select_from(ExternalIdentityBindingRow)) == 1
        assert session.scalar(select(func.count()).select_from(HumanIdentityRow)) == 1
        assert session.get(HumanIdentityRow, binding.human_identity_id) is not None
        transactions = session.scalars(select(HumanAuthTransactionRow)).all()
        assert {row.id for row in transactions} == {"hac_race_1", "hac_race_2"}
        assert all(row.state == "VERIFIED" for row in transactions)
        assert {row.resolved_human_identity_id for row in transactions} == {results[0]}
