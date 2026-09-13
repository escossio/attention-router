"""Focused service behavior with isolated SQLite transactions."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import socket

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from attention_router.config import Settings
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanAuthChallengeExpired,
    HumanAuthChallengeNotFound,
    HumanAuthCredentialRejected,
    HumanAuthDisabled,
    HumanAuthNonceMismatch,
    HumanAuthProviderUnavailable,
    VerifiedProviderIdentity,
)
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanIdentityRow,
)
from attention_router.application.human_identity import HumanIdentityService
from attention_router.application import human_identity as application


NOW = datetime(2026, 9, 13, tzinfo=UTC)
TOKEN = "synthetic-service-id-token"


class FakeVerifier:
    def __init__(self):
        self.identity = None
        self.error = None
        self.calls = 0
        self.events = []

    def verify(self, id_token: str) -> VerifiedProviderIdentity:
        assert id_token == TOKEN
        self.calls += 1
        self.events.append("VERIFY_PROVIDER")
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Human Identity service tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def session():
    from attention_router.infrastructure.db import Base
    from attention_router.infrastructure.repository import seed_policies

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _disable_sqlite_legacy_transaction_control(dbapi_connection, _):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_sqlite_begin(connection):
        connection.exec_driver_sql("BEGIN")

    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with Session() as db:
            seed_policies(db)
            db.commit()
            yield db
    finally:
        engine.dispose()


@pytest.fixture
def identity_settings():
    return Settings(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        human_identity_enabled=True,
        human_auth_challenge_ttl_seconds=300,
        google_identity_audience="server-client-id.example.apps.googleusercontent.com",
    )


@pytest.fixture
def verifier():
    return FakeVerifier()


@pytest.fixture
def service(identity_settings, verifier):
    return HumanIdentityService(settings=identity_settings, verifier=verifier)


def _verified(issued, subject="stable-google-sub", nonce=None):
    return VerifiedProviderIdentity(
        provider="google", subject=subject,
        nonce=issued.nonce if nonce is None else nonce,
        issued_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )


def _utc(value):
    # SQLite DateTime reads omit tzinfo; the stored timestamps represent UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def issued(session, service, verifier):
    result = service.issue_google_challenge(session, now=NOW)
    session.commit()
    verifier.identity = _verified(result)
    return result


@pytest.fixture
def lock_events(monkeypatch, verifier):
    real_lock = application.lock_human_auth_transaction

    def tracked_lock(session, challenge_id):
        verifier.events.append("LOCK_CHALLENGE")
        return real_lock(session, challenge_id)

    monkeypatch.setattr(application, "lock_human_auth_transaction", tracked_lock)
    return verifier.events


def test_disabled_issue_has_no_side_effects(session, service, identity_settings):
    identity_settings.human_identity_enabled = False
    with pytest.raises(HumanAuthDisabled):
        service.issue_google_challenge(session, now=NOW)
    assert _count(session, HumanAuthTransactionRow) == 0


def test_disabled_verify_precedes_lookup_and_provider(session, service, identity_settings, verifier):
    identity_settings.human_identity_enabled = False
    with pytest.raises(HumanAuthDisabled):
        service.verify_google_challenge(session, "hac_missing", TOKEN, now=NOW)
    assert verifier.calls == 0
    assert _count(session, HumanAuthTransactionRow) == 0


def test_issue_persists_only_nonce_digest_with_default_ttl(session, service):
    issued = service.issue_google_challenge(session, now=NOW)
    assert issued.challenge_id.startswith("hac_")
    assert len(issued.nonce) >= 32
    assert issued.expires_at == NOW + timedelta(seconds=300)
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.id == issued.challenge_id
    assert row.provider == "google"
    assert row.state == "PENDING"
    assert _utc(row.created_at) == NOW
    assert _utc(row.expires_at) == issued.expires_at
    assert row.consumed_at is None
    assert row.resolved_human_identity_id is None
    assert row.nonce_digest == hashlib.sha256(issued.nonce.encode("utf-8")).hexdigest()
    assert issued.nonce not in vars(row).values()


def test_challenges_have_distinct_ids_and_nonces(session, service):
    first = service.issue_google_challenge(session, now=NOW)
    second = service.issue_google_challenge(session, now=NOW)
    assert first.challenge_id != second.challenge_id
    assert first.nonce != second.nonce


def test_missing_challenge_does_not_call_provider(session, service, verifier):
    with pytest.raises(HumanAuthChallengeNotFound):
        service.verify_google_challenge(session, "hac_missing", TOKEN, now=NOW)
    assert verifier.calls == 0


@pytest.mark.parametrize("elapsed", [300, 301])
def test_expired_challenge_remains_pending(session, service, issued, verifier, elapsed):
    with pytest.raises(HumanAuthChallengeExpired):
        service.verify_google_challenge(
            session, issued.challenge_id, TOKEN, now=NOW + timedelta(seconds=elapsed),
        )
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "PENDING"
    assert row.consumed_at is None
    assert verifier.calls == 0


@pytest.mark.parametrize("state", ["VERIFIED", "REJECTED"])
def test_consumed_challenge_precedes_expiry_and_provider(session, service, issued, verifier, state):
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    row.state = state
    row.consumed_at = NOW
    session.commit()
    with pytest.raises(HumanAuthChallengeConsumed):
        service.verify_google_challenge(
            session, issued.challenge_id, TOKEN, now=NOW + timedelta(minutes=10),
        )
    assert verifier.calls == 0
    assert row.state == state


def test_provider_unavailable_preserves_pending_without_lock(
    session, service, issued, verifier, lock_events,
):
    original = HumanAuthProviderUnavailable()
    verifier.error = original
    with session.begin():
        with pytest.raises(HumanAuthProviderUnavailable) as error:
            service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
        assert error.value is original
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "PENDING"
    assert row.consumed_at is None
    assert row.resolved_human_identity_id is None
    assert lock_events == ["VERIFY_PROVIDER"]
    assert _count(session, HumanIdentityRow) == 0


def test_credential_rejection_can_be_committed_by_caller(
    session, service, issued, verifier, lock_events,
):
    original = HumanAuthCredentialRejected()
    verifier.error = original
    with session.begin():
        with pytest.raises(HumanAuthCredentialRejected) as error:
            service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
        assert error.value is original
    session.expire_all()
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "REJECTED"
    assert _utc(row.consumed_at) == NOW
    assert row.resolved_human_identity_id is None
    assert _count(session, HumanIdentityRow) == 0
    assert lock_events == ["VERIFY_PROVIDER", "LOCK_CHALLENGE"]


def test_nonce_mismatch_can_be_committed_as_rejected(session, service, issued, verifier):
    verifier.identity = _verified(issued, nonce="synthetic-wrong-nonce")
    with session.begin():
        with pytest.raises(HumanAuthNonceMismatch) as error:
            service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
        assert error.value.args == ()
    session.expire_all()
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "REJECTED"
    assert _utc(row.consumed_at) == NOW
    assert row.resolved_human_identity_id is None
    assert _count(session, HumanIdentityRow) == 0


def test_success_is_opaque_single_use_and_provider_precedes_lock(
    session, service, issued, verifier, lock_events,
):
    with session.begin():
        result = service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
    assert result.human_identity_id.startswith("hid_")
    assert asdict(result) == {
        "status": "HUMAN_IDENTITY_VALIDATED", "human_identity_id": result.human_identity_id,
    }
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "VERIFIED"
    assert _utc(row.consumed_at) == NOW
    assert row.resolved_human_identity_id == result.human_identity_id
    binding = session.scalars(select(ExternalIdentityBindingRow)).one()
    assert (binding.provider, binding.subject, binding.human_identity_id) == (
        "google", "stable-google-sub", result.human_identity_id,
    )
    assert lock_events == ["VERIFY_PROVIDER", "LOCK_CHALLENGE"]
    for model in (HumanAuthTransactionRow, ExternalIdentityBindingRow, HumanIdentityRow):
        for stored in session.scalars(select(model)):
            values = [getattr(stored, column.key) for column in model.__table__.columns]
            assert TOKEN not in values
            assert issued.nonce not in values
    with pytest.raises(HumanAuthChallengeConsumed):
        service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
    assert verifier.calls == 1
    assert lock_events == ["VERIFY_PROVIDER", "LOCK_CHALLENGE"]


@pytest.mark.parametrize("second_subject", ["same-google-sub", "different-google-sub"])
def test_identity_is_keyed_by_stable_subject(session, service, verifier, second_subject):
    with session.begin():
        first = service.issue_google_challenge(session, now=NOW)
        verifier.identity = _verified(first, subject="same-google-sub")
        result_a = service.verify_google_challenge(session, first.challenge_id, TOKEN, now=NOW)
    with session.begin():
        second = service.issue_google_challenge(session, now=NOW)
        verifier.identity = _verified(second, subject=second_subject)
        result_b = service.verify_google_challenge(session, second.challenge_id, TOKEN, now=NOW)
    assert (result_a.human_identity_id == result_b.human_identity_id) is (
        second_subject == "same-google-sub"
    )


@pytest.mark.parametrize("provider_rejects", [False, True])
@pytest.mark.parametrize("state", ["VERIFIED", "REJECTED"])
def test_locked_consumed_state_wins_over_provider_result(
    session, service, issued, verifier, monkeypatch, provider_rejects, state,
):
    if provider_rejects:
        verifier.error = HumanAuthCredentialRejected()
    real_lock = application.lock_human_auth_transaction
    locked_rows = []

    def intervening_lock(session, challenge_id):
        row = real_lock(session, challenge_id)
        row.state = state
        row.consumed_at = NOW - timedelta(seconds=1)
        locked_rows.append(row)
        return row

    monkeypatch.setattr(application, "lock_human_auth_transaction", intervening_lock)
    with session.begin():
        with pytest.raises(HumanAuthChallengeConsumed):
            service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
        assert locked_rows[0].state == state
        assert locked_rows[0].consumed_at == NOW - timedelta(seconds=1)
        assert locked_rows[0].resolved_human_identity_id is None
    assert verifier.calls == 1
    assert _count(session, HumanIdentityRow) == 0


@pytest.mark.parametrize("provider_rejects", [False, True])
def test_locked_expiry_wins_over_provider_result(
    session, service, issued, verifier, monkeypatch, provider_rejects,
):
    if provider_rejects:
        verifier.error = HumanAuthCredentialRejected()
    real_lock = application.lock_human_auth_transaction

    def expired_lock(session, challenge_id):
        row = real_lock(session, challenge_id)
        row.expires_at = NOW
        return row

    monkeypatch.setattr(application, "lock_human_auth_transaction", expired_lock)
    with session.begin():
        with pytest.raises(HumanAuthChallengeExpired):
            service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "PENDING"
    assert row.consumed_at is None
    assert _count(session, HumanIdentityRow) == 0


@pytest.mark.parametrize("provider_rejects", [False, True])
def test_real_time_is_read_again_after_provider_and_lock(
    session, service, issued, verifier, monkeypatch, provider_rejects,
):
    if provider_rejects:
        verifier.error = HumanAuthCredentialRejected()
    moments = iter([NOW + timedelta(seconds=1), issued.expires_at])
    observations = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            observations.append(list(verifier.events))
            return next(moments)

    real_lock = application.lock_human_auth_transaction

    def tracked_lock(session, challenge_id):
        verifier.events.append("LOCK_CHALLENGE")
        return real_lock(session, challenge_id)

    monkeypatch.setattr(application, "datetime", Clock)
    monkeypatch.setattr(application, "lock_human_auth_transaction", tracked_lock)
    with session.begin():
        with pytest.raises(HumanAuthChallengeExpired):
            service.verify_google_challenge(session, issued.challenge_id, TOKEN)
    assert observations == [[], ["VERIFY_PROVIDER", "LOCK_CHALLENGE"]]
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "PENDING"
    assert row.consumed_at is None


def test_explicit_time_is_deterministic_and_default_issue_uses_utc(
    session, service, verifier, monkeypatch,
):
    clock_calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            clock_calls.append(tz)
            return NOW

    monkeypatch.setattr(application, "datetime", Clock)
    with session.begin():
        issued = service.issue_google_challenge(session)
        assert issued.expires_at == NOW + timedelta(seconds=300)
        verifier.identity = _verified(issued)
        service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
    assert clock_calls == [UTC]


@pytest.mark.parametrize("outcome", ["valid", "rejected", "unavailable", "mismatch"])
def test_service_never_calls_caller_commit_or_rollback(
    session, service, verifier, monkeypatch, outcome,
):
    def forbidden():
        pytest.fail("The service must leave commit and rollback to its caller")

    with session.begin():
        with monkeypatch.context() as patch:
            patch.setattr(session, "commit", forbidden)
            patch.setattr(session, "rollback", forbidden)
            issued = service.issue_google_challenge(session, now=NOW)
            verifier.identity = _verified(issued)
            expected = {
                "rejected": HumanAuthCredentialRejected,
                "unavailable": HumanAuthProviderUnavailable,
                "mismatch": HumanAuthNonceMismatch,
            }.get(outcome)
            if outcome in {"rejected", "unavailable"}:
                verifier.error = expected()
            elif outcome == "mismatch":
                verifier.identity = _verified(issued, nonce="other-nonce")
            if expected is None:
                service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
            else:
                with pytest.raises(expected):
                    service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)


def test_caller_can_rollback_issuance(session, service):
    issued = service.issue_google_challenge(session, now=NOW)
    session.rollback()
    assert session.get(HumanAuthTransactionRow, issued.challenge_id) is None


@pytest.mark.parametrize("outcome", ["valid", "rejected", "mismatch"])
def test_caller_can_rollback_verification_of_existing_challenge(
    session, service, issued, verifier, outcome,
):
    if outcome == "rejected":
        verifier.error = HumanAuthCredentialRejected()
    elif outcome == "mismatch":
        verifier.identity = _verified(issued, nonce="other-nonce")
    expected = {"rejected": HumanAuthCredentialRejected, "mismatch": HumanAuthNonceMismatch}.get(outcome)
    with pytest.raises(RuntimeError, match="caller cancels"):
        with session.begin():
            if expected is None:
                service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
            else:
                with pytest.raises(expected):
                    service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
            session.flush()
            raise RuntimeError("caller cancels")
    row = session.get(HumanAuthTransactionRow, issued.challenge_id)
    assert row.state == "PENDING"
    assert row.consumed_at is None
    assert row.resolved_human_identity_id is None
    assert _count(session, HumanIdentityRow) == 0
    assert _count(session, ExternalIdentityBindingRow) == 0
