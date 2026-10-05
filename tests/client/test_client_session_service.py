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

    directory = service.authenticated_tenant_directory(
        session,
        session_token=issued.session_token,
        now=NOW + timedelta(seconds=2),
    )
    assert directory.active_tenant_id == TENANT_ID
    assert len(directory.memberships) == 1
    assert directory.memberships[0].membership_id == "ctm_" + "m" * 24
    assert directory.memberships[0].tenant_id == TENANT_ID
    assert directory.memberships[0].display_name == "Personal"
    assert directory.memberships[0].role.value == "OWNER"


def test_tenant_directory_normalizes_display_name_without_changing_authority(session):
    private, spki, public = keypair()
    seed(session, spki)
    tenant = session.get(TenantRow, TENANT_ID)
    tenant.name = "  Personal\n\tContext  "
    session.commit()

    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=TENANT_ID,
        now=NOW,
    )
    issued = service.complete_session(
        session,
        session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )
    session.commit()

    directory = service.authenticated_tenant_directory(
        session,
        session_token=issued.session_token,
        now=NOW + timedelta(seconds=2),
    )

    assert directory.memberships[0].display_name == "Personal Context"
    assert directory.memberships[0].membership_id == "ctm_" + "m" * 24
    assert directory.memberships[0].tenant_id == TENANT_ID


def test_tenant_directory_fails_closed_for_unavailable_secondary_tenant(session):
    private, spki, public = keypair()
    seed(session, spki)
    secondary_tenant_id = "tnt_" + "o" * 24
    session.add(
        TenantRow(
            id=secondary_tenant_id,
            slug="secondary-synthetic",
            name="Secondary",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add(
        ClientTenantMembershipRow(
            id="ctm_" + "n" * 24,
            human_identity_id=HUMAN_ID,
            tenant_id=secondary_tenant_id,
            role="OWNER",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()

    service = ClientSessionService(settings=settings())
    challenge = service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=TENANT_ID,
        now=NOW,
    )
    issued = service.complete_session(
        session,
        session_challenge_id=challenge.challenge_id
            if hasattr(challenge, "challenge_id") else challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )
    session.commit()

    secondary = session.get(TenantRow, secondary_tenant_id)
    secondary.status = "DISABLED"
    session.commit()

    with pytest.raises(ClientSessionAuthorityRejected):
        service.authenticated_tenant_directory(
            session,
            session_token=issued.session_token,
            now=NOW + timedelta(seconds=2),
        )


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
