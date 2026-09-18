"""PostgreSQL concurrency proof for the V0.3B bootstrap authority transition."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from attention_router.application.client_bootstrap import (
    DeviceBootstrapChallengeConsumed,
    DeviceBootstrapDeviceConflict,
    DeviceBootstrapGrantRejected,
    DeviceBootstrapService,
    DeviceBootstrapSignatureInvalid,
)
from attention_router.config import Settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
    DeviceBootstrapChallengeRow,
)
from attention_router.infrastructure.human_identity_models import (
    HumanAuthContinuationGrantRow,
    HumanAuthTransactionRow,
    HumanIdentityRow,
)
from attention_router.infrastructure.models import TenantRow
from attention_router.security.device_keys import encode_unpadded_base64url


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
HUMAN_ID = "hid_" + "h" * 24


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        human_identity_enabled=True,
        google_identity_audience="synthetic-client-id.example",
        device_bootstrap_enabled=True,
        device_bootstrap_challenge_ttl_seconds=300,
    )


def _service() -> DeviceBootstrapService:
    return DeviceBootstrapService(settings=_settings())


def _device_key():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, encode_unpadded_base64url(spki)


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(private_key, challenge: str) -> str:
    return encode_unpadded_base64url(
        private_key.sign(_decode(challenge), ec.ECDSA(hashes.SHA256()))
    )


def _seed_human(Session, human_identity_id=HUMAN_ID):
    with Session.begin() as session:
        if session.get(HumanIdentityRow, human_identity_id) is None:
            session.add(HumanIdentityRow(
                id=human_identity_id,
                created_at=NOW,
            ))


def _seed_grant(Session, *, suffix: str, human_identity_id=HUMAN_ID) -> str:
    token = "hcg_" + (suffix * 43)[:43]
    transaction_id = "hac_" + (suffix * 24)[:24]
    with Session.begin() as session:
        session.add(HumanAuthTransactionRow(
            id=transaction_id,
            provider="google",
            nonce_digest=hashlib.sha256(("nonce-" + suffix).encode()).hexdigest(),
            state="VERIFIED",
            created_at=NOW - timedelta(seconds=2),
            expires_at=NOW + timedelta(minutes=5),
            consumed_at=NOW - timedelta(seconds=1),
            resolved_human_identity_id=human_identity_id,
        ))
        session.add(HumanAuthContinuationGrantRow(
            id="hcgi_" + (suffix * 24)[:24],
            token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
            human_identity_id=human_identity_id,
            source_auth_transaction_id=transaction_id,
            purpose="DEVICE_BOOTSTRAP",
            state="ACTIVE",
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            consumed_at=None,
            revoked_at=None,
        ))
    return token


def _start(Session, token: str, spki: str, *, name="Synthetic Android"):
    service = _service()
    with Session.begin() as session:
        return service.start_device_bootstrap(
            session,
            continuation_token=token,
            public_key_spki_b64url=spki,
            canonical_device_name=name,
            platform="ANDROID",
            roles=["CLIENT", "CAPABILITY_NODE"],
            now=NOW,
        )


def _counts(Session):
    with Session() as session:
        return {
            "memberships": session.scalar(
                select(func.count()).select_from(ClientTenantMembershipRow)
            ),
            "devices": session.scalar(
                select(func.count()).select_from(ClientDeviceRow)
            ),
            "personal_tenants": session.scalar(
                select(func.count())
                .select_from(TenantRow)
                .where(TenantRow.id != DEFAULT_TENANT_ID)
            ),
        }


def test_same_grant_and_challenge_complete_exactly_once(Session):
    _seed_human(Session)
    token = _seed_grant(Session, suffix="a")
    private, spki = _device_key()
    challenge = _start(Session, token, spki)
    signature = _sign(private, challenge.challenge_b64url)
    barrier = threading.Barrier(2)

    def complete():
        service = _service()
        with Session.begin() as session:
            barrier.wait()
            try:
                return service.complete_device_bootstrap(
                    session,
                    bootstrap_challenge_id=challenge.bootstrap_challenge_id,
                    device_signature_b64url=signature,
                    now=NOW + timedelta(seconds=1),
                )
            except (DeviceBootstrapChallengeConsumed, DeviceBootstrapGrantRejected) as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: complete(), range(2)))

    successes = [item for item in results if not isinstance(item, str)]
    failures = [item for item in results if isinstance(item, str)]
    assert len(successes) == 1
    assert len(failures) == 1

    with Session() as session:
        grant = session.scalar(select(HumanAuthContinuationGrantRow))
        row = session.get(DeviceBootstrapChallengeRow, challenge.bootstrap_challenge_id)
        assert grant.state == "CONSUMED"
        assert grant.consumed_at is not None
        assert row.state == "VERIFIED"
        assert row.verified_at is not None

    assert _counts(Session) == {
        "memberships": 1,
        "devices": 1,
        "personal_tenants": 1,
    }


def test_concurrent_first_bootstraps_converge_on_one_personal_tenant_and_owner(Session):
    _seed_human(Session)
    token_a = _seed_grant(Session, suffix="b")
    token_b = _seed_grant(Session, suffix="c")
    private_a, spki_a = _device_key()
    private_b, spki_b = _device_key()
    challenge_a = _start(Session, token_a, spki_a, name="Synthetic A")
    challenge_b = _start(Session, token_b, spki_b, name="Synthetic B")
    barrier = threading.Barrier(2)

    def complete(value):
        challenge, private = value
        service = _service()
        with Session.begin() as session:
            barrier.wait()
            return service.complete_device_bootstrap(
                session,
                bootstrap_challenge_id=challenge.bootstrap_challenge_id,
                device_signature_b64url=_sign(private, challenge.challenge_b64url),
                now=NOW + timedelta(seconds=1),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(
            complete,
            ((challenge_a, private_a), (challenge_b, private_b)),
        ))

    assert first.initial_tenant_id == second.initial_tenant_id
    assert first.memberships[0].tenant_id == second.memberships[0].tenant_id
    assert first.memberships[0].role.value == second.memberships[0].role.value == "OWNER"

    with Session() as session:
        memberships = session.scalars(
            select(ClientTenantMembershipRow)
            .where(ClientTenantMembershipRow.human_identity_id == HUMAN_ID)
        ).all()
        grants = session.scalars(select(HumanAuthContinuationGrantRow)).all()
        assert len(memberships) == 1
        assert memberships[0].role == "OWNER"
        assert memberships[0].status == "ACTIVE"
        assert memberships[0].tenant_id != DEFAULT_TENANT_ID
        assert len(grants) == 2
        assert {row.state for row in grants} == {"CONSUMED"}

    assert _counts(Session) == {
        "memberships": 1,
        "devices": 2,
        "personal_tenants": 1,
    }


def test_concurrent_same_key_enrollment_converges_on_one_client_device(Session):
    _seed_human(Session)
    token_a = _seed_grant(Session, suffix="d")
    token_b = _seed_grant(Session, suffix="e")
    private, spki = _device_key()
    challenge_a = _start(Session, token_a, spki, name="Synthetic Same")
    challenge_b = _start(Session, token_b, spki, name="Synthetic Same")
    barrier = threading.Barrier(2)

    def complete(challenge):
        service = _service()
        with Session.begin() as session:
            barrier.wait()
            return service.complete_device_bootstrap(
                session,
                bootstrap_challenge_id=challenge.bootstrap_challenge_id,
                device_signature_b64url=_sign(private, challenge.challenge_b64url),
                now=NOW + timedelta(seconds=1),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(complete, (challenge_a, challenge_b)))

    assert first.device.device_id == second.device.device_id
    assert first.device.public_key_fingerprint == second.device.public_key_fingerprint
    assert _counts(Session) == {
        "memberships": 1,
        "devices": 1,
        "personal_tenants": 1,
    }


def test_wrong_signature_rolls_back_without_consuming_grant(Session):
    _seed_human(Session)
    token = _seed_grant(Session, suffix="f")
    _, spki = _device_key()
    wrong_private, _ = _device_key()
    challenge = _start(Session, token, spki)

    service = _service()
    with pytest.raises(DeviceBootstrapSignatureInvalid):
        with Session.begin() as session:
            service.complete_device_bootstrap(
                session,
                bootstrap_challenge_id=challenge.bootstrap_challenge_id,
                device_signature_b64url=_sign(wrong_private, challenge.challenge_b64url),
                now=NOW + timedelta(seconds=1),
            )

    with Session() as session:
        grant = session.scalar(select(HumanAuthContinuationGrantRow))
        row = session.get(DeviceBootstrapChallengeRow, challenge.bootstrap_challenge_id)
        assert grant.state == "ACTIVE"
        assert grant.consumed_at is None
        assert row.state == "PENDING"
    assert _counts(Session) == {
        "memberships": 0,
        "devices": 0,
        "personal_tenants": 0,
    }


def test_same_device_fingerprint_cannot_cross_human_identity(Session):
    human_b = "hid_" + "z" * 24
    _seed_human(Session, HUMAN_ID)
    _seed_human(Session, human_b)
    token_a = _seed_grant(Session, suffix="g", human_identity_id=HUMAN_ID)
    token_b = _seed_grant(Session, suffix="i", human_identity_id=human_b)
    private, spki = _device_key()

    first_challenge = _start(Session, token_a, spki, name="Synthetic Owner A")
    with Session.begin() as session:
        first = _service().complete_device_bootstrap(
            session,
            bootstrap_challenge_id=first_challenge.bootstrap_challenge_id,
            device_signature_b64url=_sign(private, first_challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )

    second_challenge = _start(Session, token_b, spki, name="Synthetic Owner B")
    with pytest.raises(DeviceBootstrapDeviceConflict):
        with Session.begin() as session:
            _service().complete_device_bootstrap(
                session,
                bootstrap_challenge_id=second_challenge.bootstrap_challenge_id,
                device_signature_b64url=_sign(private, second_challenge.challenge_b64url),
                now=NOW + timedelta(seconds=2),
            )

    with Session() as session:
        devices = session.scalars(select(ClientDeviceRow)).all()
        grant_b = session.scalar(
            select(HumanAuthContinuationGrantRow)
            .where(HumanAuthContinuationGrantRow.human_identity_id == human_b)
        )
        challenge_b = session.get(
            DeviceBootstrapChallengeRow,
            second_challenge.bootstrap_challenge_id,
        )
        assert len(devices) == 1
        assert devices[0].id == first.device.device_id
        assert devices[0].human_identity_id == HUMAN_ID
        assert grant_b.state == "ACTIVE"
        assert challenge_b.state == "PENDING"
