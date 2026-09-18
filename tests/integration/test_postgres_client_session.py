"""PostgreSQL concurrency proof for V0.3C client-session authority."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from attention_router.application.client_session import (
    ClientSessionAuthorityRejected, ClientSessionChallengeConflict,
    ClientSessionChallengeConsumed, ClientSessionService,
)
from attention_router.config import Settings
from attention_router.infrastructure.client_bootstrap_models import ClientDeviceRow, ClientTenantMembershipRow
from attention_router.infrastructure.client_session_models import ClientSessionChallengeRow, ClientSessionRow
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.security.device_keys import encode_unpadded_base64url

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 18, 17, 30, tzinfo=UTC)
HUMAN_ID, TENANT_ID, DEVICE_ID = "hid_" + "h" * 24, "tnt_" + "t" * 24, "cdev_" + "d" * 24


def settings() -> Settings:
    return Settings(
        _env_file=None, app_env="test", admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32, client_session_enabled=True,
        client_session_challenge_ttl_seconds=300, client_session_ttl_seconds=900,
    )


def service() -> ClientSessionService:
    return ClientSessionService(settings=settings())


def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, spki, encode_unpadded_base64url(spki)


def sign(private, challenge: str) -> str:
    raw = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    return encode_unpadded_base64url(private.sign(raw, ec.ECDSA(hashes.SHA256())))


def seed(Session, spki: bytes):
    with Session.begin() as session:
        session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
        session.add(TenantRow(
            id=TENANT_ID, slug="personal-session-proof", name="Personal", status="ACTIVE",
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
            public_key_spki=spki, canonical_name="Physical-like Android", platform="ANDROID",
            roles=["CAPABILITY_NODE", "CLIENT"], status="ACTIVE", created_at=NOW, updated_at=NOW,
        ))


def start(Session, public):
    with Session.begin() as session:
        return service().start_session(
            session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
        )


def test_same_challenge_complete_is_exactly_once(Session):
    private, spki, public = keypair()
    seed(Session, spki)
    challenge, barrier = start(Session, public), threading.Barrier(2)
    signature = sign(private, challenge.challenge_b64url)
    def complete(_):
        with Session.begin() as session:
            barrier.wait()
            try:
                return service().complete_session(
                    session, session_challenge_id=challenge.session_challenge_id,
                    device_signature_b64url=signature, now=NOW + timedelta(seconds=1),
                )
            except ClientSessionChallengeConsumed:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, range(2)))
    assert sum(item is not None for item in results) == 1
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(ClientSessionRow)) == 1
        assert session.get(ClientSessionChallengeRow, challenge.session_challenge_id).state == "VERIFIED"


def test_concurrent_session_starts_allow_only_one_pending_challenge(Session):
    _, spki, public = keypair()
    seed(Session, spki)
    barrier = threading.Barrier(2)
    def create(_):
        with Session.begin() as session:
            barrier.wait()
            try:
                return service().start_session(
                    session, public_key_spki_b64url=public, requested_tenant_id=None, now=NOW
                )
            except ClientSessionChallengeConflict:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(item is not None for item in results) == 1
    with Session() as session:
        assert session.scalar(select(func.count()).select_from(ClientSessionChallengeRow)) == 1


@pytest.mark.parametrize(("target", "new_status"), [("membership", "REVOKED"), ("device", "REVOKED")])
def test_current_authority_revocation_denies_existing_unexpired_bearer(Session, target, new_status):
    private, spki, public = keypair()
    seed(Session, spki)
    challenge = start(Session, public)
    with Session.begin() as session:
        issued = service().complete_session(
            session, session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=sign(private, challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )
    with Session.begin() as session:
        row = session.scalar(select(ClientTenantMembershipRow)) if target == "membership" else session.get(ClientDeviceRow, DEVICE_ID)
        row.status = new_status
    with Session() as session:
        with pytest.raises(ClientSessionAuthorityRejected):
            service().authenticated_bootstrap(
                session, session_token=issued.session_token, now=NOW + timedelta(seconds=2)
            )
