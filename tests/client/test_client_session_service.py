from datetime import UTC, datetime, timedelta
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionChallengeConflict,
    ClientSessionChallengeConsumed,
    ClientSessionService,
    ClientSessionSignatureInvalid,
)
from attention_router.config import Settings
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow, ClientTenantMembershipRow
from attention_router.infrastructure.client_session_models import ClientSessionChallengeRow, ClientSessionRow
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.security.device_keys import encode_unpadded_base64url

NOW = datetime(2026, 9, 18, 16, 30, tzinfo=UTC)
HUMAN_ID = "hid_" + "h" * 24
TENANT_ID = "tnt_" + "t" * 24
DEVICE_ID = "cdev_" + "d" * 24


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as value:
        yield value
    engine.dispose()


def settings() -> Settings:
    return Settings(
        _env_file=None, app_env="test", admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32, client_session_enabled=True,
        client_session_challenge_ttl_seconds=300, client_session_ttl_seconds=900,
    )


def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, spki, encode_unpadded_base64url(spki)


def sign(private, challenge: str) -> str:
    raw = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    return encode_unpadded_base64url(private.sign(raw, ec.ECDSA(hashes.SHA256())))


def seed(session, spki: bytes):
    session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
    session.add(TenantRow(
        id=TENANT_ID, slug="personal-synthetic", name="Personal", status="ACTIVE",
        created_at=NOW, updated_at=NOW,
    ))
    session.flush()
    session.add(ClientTenantMembershipRow(
        id="ctm_" + "m" * 24, human_identity_id=HUMAN_ID, tenant_id=TENANT_ID,
        role="OWNER", status="ACTIVE", created_at=NOW, updated_at=NOW,
    ))
    session.add(ClientDeviceRow(
        id=DEVICE_ID, human_identity_id=HUMAN_ID,
        public_key_fingerprint="sha256:" + hashlib.sha256(spki).hexdigest(),
        public_key_spki=spki, canonical_name="Synthetic Android", platform="ANDROID",
        roles=["CAPABILITY_NODE", "CLIENT"], status="ACTIVE", created_at=NOW, updated_at=NOW,
    ))
    session.commit()


def test_session_issue_persists_digest_only_and_authenticated_bootstrap(session):
    private, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
    )
    issued = service.complete_session(
        session, session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )
    session.commit()
    assert issued.session_token.startswith("cst_")
    assert issued.session_token not in repr(issued)
    row = session.scalar(select(ClientSessionRow))
    assert row.token_digest == hashlib.sha256(issued.session_token.encode("ascii")).hexdigest()
    assert not hasattr(row, "session_token")
    snapshot = service.authenticated_bootstrap(
        session, session_token=issued.session_token, now=NOW + timedelta(seconds=2)
    )
    assert snapshot.active_tenant_id == TENANT_ID
    assert snapshot.device.device_id == DEVICE_ID


def test_wrong_signature_keeps_challenge_pending_and_creates_no_session(session):
    _, spki, public = keypair()
    wrong, _, _ = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
    )
    session.commit()
    with pytest.raises(ClientSessionSignatureInvalid):
        service.complete_session(
            session, session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=sign(wrong, challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )
    session.rollback()
    row = session.get(ClientSessionChallengeRow, challenge.session_challenge_id)
    assert row.state == "PENDING"
    assert session.scalar(select(func.count()).select_from(ClientSessionRow)) == 0


def test_same_challenge_cannot_issue_twice(session):
    private, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
    )
    signature = sign(private, challenge.challenge_b64url)
    service.complete_session(
        session, session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=signature, now=NOW + timedelta(seconds=1),
    )
    session.commit()
    with pytest.raises(ClientSessionChallengeConsumed):
        service.complete_session(
            session, session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=signature, now=NOW + timedelta(seconds=2),
        )


def test_same_context_retry_returns_same_pending_challenge_and_completes(session):
    private, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    first = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=TENANT_ID, now=NOW
    )
    session.commit()

    retry = service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=TENANT_ID,
        now=NOW + timedelta(seconds=1),
    )
    session.commit()

    assert first == retry
    assert first.session_challenge_id.startswith("csc_r1_")
    row = session.get(ClientSessionChallengeRow, first.session_challenge_id)
    raw = base64.urlsafe_b64decode(first.challenge_b64url + "=")
    assert row.challenge_digest == hashlib.sha256(raw).hexdigest()
    assert row.state == "PENDING"
    assert session.scalar(select(func.count()).select_from(ClientSessionChallengeRow)) == 1

    issued = service.complete_session(
        session,
        session_challenge_id=retry.session_challenge_id,
        device_signature_b64url=sign(private, retry.challenge_b64url),
        now=NOW + timedelta(seconds=2),
    )
    session.commit()
    assert issued.tenant_id == TENANT_ID


def test_retryable_challenge_derivation_has_stable_domain_vector(session, monkeypatch):
    _, spki, public = keypair()
    seed(session, spki)
    entropy = bytes(range(32))
    monkeypatch.setattr(
        "attention_router.application.client_session.secrets.token_bytes",
        lambda size: entropy if size == 32 else None,
    )

    challenge = ClientSessionService(settings=settings()).start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
    )

    assert challenge.session_challenge_id == (
        "csc_r1_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    )
    assert challenge.challenge_b64url == "iyiTGXN74DDOEMbkf-P1yhusxdJ1cr3YVkf40tjDAWQ"
    row = session.get(ClientSessionChallengeRow, challenge.session_challenge_id)
    assert row.challenge_digest == (
        "3c810af648c2eab9fa84341212c8761fef3f76d2f0ae92cdbc25a4cee6e2dce9"
    )


def test_different_tenant_context_does_not_reuse_pending_challenge(session):
    _, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    first = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=TENANT_ID, now=NOW
    )
    session.commit()

    with pytest.raises(ClientSessionChallengeConflict):
        service.start_session(
            session,
            public_key_spki_b64url=public,
            requested_tenant_id="tnt_" + "o" * 24,
            now=NOW + timedelta(seconds=1),
        )
    session.rollback()

    row = session.get(ClientSessionChallengeRow, first.session_challenge_id)
    assert row.state == "PENDING"
    assert row.requested_tenant_id == TENANT_ID
    assert session.scalar(select(func.count()).select_from(ClientSessionChallengeRow)) == 1


def test_legacy_pending_challenge_conflicts_until_expiry_then_is_replaced(session):
    _, spki, public = keypair()
    seed(session, spki)
    legacy = ClientSessionChallengeRow(
        # A legacy 32-character suffix can begin with "r1_"; exact length keeps
        # it distinct from the new 43-character retryable suffix.
        id="csc_r1_" + "l" * 29,
        device_id=DEVICE_ID,
        human_identity_id=HUMAN_ID,
        requested_tenant_id=TENANT_ID,
        challenge_digest="a" * 64,
        state="PENDING",
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=1),
        verified_at=None,
        rejected_at=None,
    )
    session.add(legacy)
    session.commit()
    service = ClientSessionService(settings=settings())

    with pytest.raises(ClientSessionChallengeConflict):
        service.start_session(
            session,
            public_key_spki_b64url=public,
            requested_tenant_id=TENANT_ID,
            now=NOW,
        )
    session.rollback()
    assert session.get(ClientSessionChallengeRow, legacy.id).state == "PENDING"

    replacement = service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=TENANT_ID,
        now=NOW + timedelta(seconds=2),
    )
    session.commit()
    assert replacement.session_challenge_id.startswith("csc_r1_")
    assert session.get(ClientSessionChallengeRow, legacy.id).state == "REJECTED"


def test_retry_fails_closed_when_retryable_challenge_digest_is_tampered(session):
    private, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=TENANT_ID, now=NOW
    )
    session.commit()
    row = session.get(ClientSessionChallengeRow, challenge.session_challenge_id)
    row.challenge_digest = "0" * 64
    session.commit()

    with pytest.raises(ClientSessionChallengeConflict):
        service.start_session(
            session,
            public_key_spki_b64url=public,
            requested_tenant_id=TENANT_ID,
            now=NOW + timedelta(seconds=1),
        )
    session.rollback()
    with pytest.raises(ClientSessionSignatureInvalid):
        service.complete_session(
            session,
            session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=sign(private, challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )
    session.rollback()
    assert session.scalar(select(func.count()).select_from(ClientSessionRow)) == 0


def test_membership_suspension_invalidates_unexpired_session(session):
    private, spki, public = keypair()
    seed(session, spki)
    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
    )
    issued = service.complete_session(
        session, session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )
    session.commit()
    membership = session.scalar(select(ClientTenantMembershipRow))
    membership.status = "SUSPENDED"
    session.commit()
    with pytest.raises(ClientSessionAuthorityRejected):
        service.authenticated_bootstrap(
            session, session_token=issued.session_token, now=NOW + timedelta(seconds=2)
        )
