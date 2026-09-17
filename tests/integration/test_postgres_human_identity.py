"""End-to-end PostgreSQL proof for the isolated Human Identity boundary."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import threading

import pytest
from sqlalchemy import MetaData, func, inspect, select

from attention_router.application.human_identity import HumanIdentityService
from attention_router.config import Settings
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanAuthContinuationGrantRejected,
    VerifiedProviderIdentity,
)
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanIdentityRow,
    HumanAuthContinuationGrantRow,
)


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 13, tzinfo=UTC)
TOKEN = "synthetic-id-token-that-must-never-be-persisted"


class FakeVerifier:
    def __init__(self):
        self._claims = {}
        self._lock = threading.Lock()
        self.calls = 0

    def add(self, token, *, subject, nonce, email):
        self._claims[token] = (subject, nonce, email)

    def verify(self, id_token):
        with self._lock:
            self.calls += 1
        subject, nonce, _email = self._claims[id_token]
        return VerifiedProviderIdentity(
            provider="google",
            subject=subject,
            nonce=nonce,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )


def _service(verifier):
    return HumanIdentityService(
        settings=Settings(
            app_env="test",
            admin_auth_enabled=False,
            internal_ingress_hmac_secret="x" * 32,
            human_identity_enabled=True,
            google_identity_audience="synthetic-client-id",
        ),
        verifier=verifier,
    )


def _issue(Session, service):
    with Session.begin() as session:
        return service.issue_google_challenge(session, now=NOW)


def _boundary_counts(engine):
    names = inspect(engine).get_table_names()
    selected = [
        name
        for name in names
        if any(term in name.lower() for term in ("tenant", "device", "session"))
    ]
    metadata = MetaData()
    metadata.reflect(bind=engine, only=selected)
    with engine.connect() as connection:
        return {name: connection.execute(
            select(func.count()).select_from(metadata.tables[name])
        ).scalar_one() for name in selected}


def test_same_challenge_is_consumed_once_and_has_one_effective_validation(Session):
    verifier = FakeVerifier()
    service = _service(verifier)
    issued = _issue(Session, service)
    verifier.add(
        TOKEN,
        subject="concurrent-sub",
        nonce=issued.nonce,
        email="first@example.test",
    )
    barrier = threading.Barrier(2)

    def verify():
        with Session.begin() as session:
            barrier.wait()
            try:
                return service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
            except HumanAuthChallengeConsumed:
                return "consumed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: verify(), range(2)))

    validated = [result for result in results if result != "consumed"]
    assert len(validated) == 1
    assert results.count("consumed") == 1
    with Session() as session:
        row = session.get(HumanAuthTransactionRow, issued.challenge_id)
        assert row.state == "VERIFIED"
        assert row.resolved_human_identity_id == validated[0].human_identity_id


def test_concurrent_and_subject_based_identity_resolution(Session):
    verifier = FakeVerifier()
    service = _service(verifier)
    first, second = _issue(Session, service), _issue(Session, service)
    verifier.add(
        "token-one", subject="same-sub", nonce=first.nonce, email="first@example.test"
    )
    verifier.add(
        "token-two", subject="same-sub", nonce=second.nonce, email="second@example.test"
    )
    barrier = threading.Barrier(2)

    def verify(challenge, token):
        with Session.begin() as session:
            barrier.wait()
            return service.verify_google_challenge(session, challenge.challenge_id, token, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result, second_result = list(
            executor.map(
                lambda value: verify(*value),
                ((first, "token-one"), (second, "token-two")),
            )
        )
    assert first_result.human_identity_id == second_result.human_identity_id
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(ExternalIdentityBindingRow)) == 1
        assert session.scalar(select(func.count()).select_from(HumanIdentityRow)) == 1

    same_subject = _issue(Session, service)
    different_subject = _issue(Session, service)
    verifier.add(
        "token-three",
        subject="same-sub",
        nonce=same_subject.nonce,
        email="third@example.test",
    )
    verifier.add(
        "token-four",
        subject="other-sub",
        nonce=different_subject.nonce,
        email="third@example.test",
    )
    with Session.begin() as session:
        same_subject_result = service.verify_google_challenge(
            session, same_subject.challenge_id, "token-three", now=NOW,
        )
    with Session.begin() as session:
        different_subject_result = service.verify_google_challenge(
            session, different_subject.challenge_id, "token-four", now=NOW,
        )
    assert same_subject_result.human_identity_id == first_result.human_identity_id
    assert different_subject_result.human_identity_id != first_result.human_identity_id


def test_concurrent_verify_issues_one_grant_and_consume_is_single_use(Session):
    verifier = FakeVerifier()
    service = _service(verifier)
    issued = _issue(Session, service)
    verifier.add(TOKEN, subject="continuation-sub", nonce=issued.nonce, email="unused@example.test")
    barrier = threading.Barrier(2)

    def verify():
        with Session.begin() as session:
            barrier.wait()
            try:
                return service.verify_google_challenge_and_issue_continuation_grant(
                    session, issued.challenge_id, TOKEN, now=NOW,
                )
            except HumanAuthChallengeConsumed:
                return "consumed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: verify(), range(2)))
    continued = [result for result in results if result != "consumed"]
    assert len(continued) == 1
    assert results.count("consumed") == 1
    with Session() as session:
        grants = session.scalars(select(HumanAuthContinuationGrantRow)).all()
        assert len(grants) == 1
        assert continued[0].continuation_grant.token not in vars(grants[0]).values()

    barrier = threading.Barrier(2)

    def consume():
        with Session.begin() as session:
            barrier.wait()
            try:
                return service.consume_continuation_grant(
                    session,
                    token=continued[0].continuation_grant.token,
                    expected_human_identity_id=continued[0].human_identity_id,
                    now=NOW + timedelta(seconds=1),
                )
            except HumanAuthContinuationGrantRejected:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        consumed = list(executor.map(lambda _: consume(), range(2)))
    assert consumed.count(continued[0].human_identity_id) == 1
    assert consumed.count("rejected") == 1
    with Session() as session:
        row = session.scalar(select(HumanAuthContinuationGrantRow))
        assert row.state == "CONSUMED"


def test_human_identity_does_not_persist_secrets_or_authority_side_effects(Session):
    verifier = FakeVerifier()
    service = _service(verifier)
    engine = Session.kw["bind"]
    before = _boundary_counts(engine)
    issued = _issue(Session, service)
    verifier.add(
        TOKEN,
        subject="privacy-sub",
        nonce=issued.nonce,
        email="privacy@example.test",
    )
    with Session.begin() as session:
        service.verify_google_challenge(session, issued.challenge_id, TOKEN, now=NOW)
    assert _boundary_counts(engine) == before

    with Session() as session:
        transaction = session.get(HumanAuthTransactionRow, issued.challenge_id)
        assert transaction.nonce_digest == sha256(issued.nonce.encode()).hexdigest()
        for model in (HumanIdentityRow, ExternalIdentityBindingRow, HumanAuthTransactionRow):
            for row in session.scalars(select(model)):
                for column in model.__table__.columns:
                    value = getattr(row, column.key)
                    if isinstance(value, str):
                        assert issued.nonce not in value
                        assert TOKEN not in value
