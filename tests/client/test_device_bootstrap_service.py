from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from attention_router.application.client_bootstrap import (
    DeviceBootstrapDeviceConflict,
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


NOW = datetime(2026, 9, 17, 23, 0, tzinfo=UTC)
TOKEN = "hcg_" + "a" * 43


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


@pytest.fixture
def session():
    from attention_router.infrastructure.db import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        with Session() as db:
            yield db
    finally:
        engine.dispose()


@pytest.fixture
def service():
    return DeviceBootstrapService(settings=_settings())


def _seed_grant(session, *, human_identity_id="hid_" + "h" * 24, token=TOKEN):
    session.add(HumanIdentityRow(id=human_identity_id, created_at=NOW))
    session.add(HumanAuthTransactionRow(
        id="hac_" + "t" * 24,
        provider="google",
        nonce_digest=hashlib.sha256(b"nonce").hexdigest(),
        state="VERIFIED",
        created_at=NOW - timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=NOW - timedelta(seconds=1),
        resolved_human_identity_id=human_identity_id,
    ))
    session.flush()
    row = HumanAuthContinuationGrantRow(
        id="hcgi_" + "g" * 24,
        token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        human_identity_id=human_identity_id,
        source_auth_transaction_id="hac_" + "t" * 24,
        purpose="DEVICE_BOOTSTRAP",
        state="ACTIVE",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=None,
        revoked_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _device_key():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, encode_unpadded_base64url(spki)


def _decode_b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(private_key, challenge_b64url: str) -> str:
    signature = private_key.sign(
        _decode_b64url(challenge_b64url),
        ec.ECDSA(hashes.SHA256()),
    )
    return encode_unpadded_base64url(signature)


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_start_binds_device_key_without_consuming_grant(session, service):
    grant = _seed_grant(session)
    private, spki = _device_key()

    result = service.start_device_bootstrap(
        session,
        continuation_token=TOKEN,
        public_key_spki_b64url=spki,
        canonical_device_name="Synthetic Android",
        platform="ANDROID",
        roles=["CLIENT", "CAPABILITY_NODE"],
        now=NOW,
    )

    row = session.scalar(select(DeviceBootstrapChallengeRow))
    assert result.bootstrap_challenge_id == row.id
    assert row.continuation_grant_id == grant.id
    assert row.human_identity_id == grant.human_identity_id
    assert row.state == "PENDING"
    assert row.challenge_digest == hashlib.sha256(
        _decode_b64url(result.challenge_b64url)
    ).hexdigest()
    assert _decode_b64url(result.challenge_b64url) not in vars(row).values()
    assert grant.state == "ACTIVE"
    assert grant.consumed_at is None
    assert private is not None


def test_complete_creates_personal_owner_device_and_consumes_grant(session, service):
    grant = _seed_grant(session)
    private, spki = _device_key()
    challenge = service.start_device_bootstrap(
        session,
        continuation_token=TOKEN,
        public_key_spki_b64url=spki,
        canonical_device_name="Synthetic Android",
        platform="ANDROID",
        roles=["CLIENT", "CAPABILITY_NODE"],
        now=NOW,
    )

    result = service.complete_device_bootstrap(
        session,
        bootstrap_challenge_id=challenge.bootstrap_challenge_id,
        device_signature_b64url=_sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )

    assert result.status == "DEVICE_BOOTSTRAP_ESTABLISHED"
    assert result.human_identity_id == grant.human_identity_id
    assert len(result.memberships) == 1
    assert result.memberships[0].role.value == "OWNER"
    assert result.memberships[0].status.value == "ACTIVE"
    assert result.initial_tenant_id == result.memberships[0].tenant_id
    assert result.initial_tenant_id != DEFAULT_TENANT_ID
    assert result.device.status.value == "ACTIVE"
    assert result.device.platform.value == "ANDROID"
    assert set(role.value for role in result.device.roles) == {
        "CLIENT", "CAPABILITY_NODE",
    }

    assert _count(session, ClientTenantMembershipRow) == 1
    assert _count(session, ClientDeviceRow) == 1
    assert session.get(HumanAuthContinuationGrantRow, grant.id).state == "CONSUMED"
    bootstrap = session.get(DeviceBootstrapChallengeRow, challenge.bootstrap_challenge_id)
    assert bootstrap.state == "VERIFIED"
    assert not hasattr(result, "session_id")
    assert not hasattr(result, "access_token")
    assert not hasattr(result, "refresh_token")


def test_wrong_signature_does_not_consume_or_create_authority(session, service):
    grant = _seed_grant(session)
    _, spki = _device_key()
    wrong_private, _ = _device_key()
    challenge = service.start_device_bootstrap(
        session,
        continuation_token=TOKEN,
        public_key_spki_b64url=spki,
        canonical_device_name="Synthetic Android",
        platform="ANDROID",
        roles=["CLIENT"],
        now=NOW,
    )

    with pytest.raises(DeviceBootstrapSignatureInvalid):
        service.complete_device_bootstrap(
            session,
            bootstrap_challenge_id=challenge.bootstrap_challenge_id,
            device_signature_b64url=_sign(wrong_private, challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )

    assert grant.state == "ACTIVE"
    assert grant.consumed_at is None
    assert _count(session, ClientTenantMembershipRow) == 0
    assert _count(session, ClientDeviceRow) == 0
    assert session.get(
        DeviceBootstrapChallengeRow,
        challenge.bootstrap_challenge_id,
    ).state == "PENDING"


def test_multiple_existing_memberships_defer_active_tenant_choice(session, service):
    grant = _seed_grant(session)
    for suffix, role in (("a", "OWNER"), ("b", "MEMBER")):
        tenant = TenantRow(
            id=f"tnt_{suffix}",
            slug=f"synthetic-{suffix}",
            name=f"Synthetic {suffix}",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(tenant)
        session.flush()
        session.add(ClientTenantMembershipRow(
            id=f"ctm_{suffix}",
            human_identity_id=grant.human_identity_id,
            tenant_id=tenant.id,
            role=role,
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        ))
    session.flush()

    private, spki = _device_key()
    challenge = service.start_device_bootstrap(
        session,
        continuation_token=TOKEN,
        public_key_spki_b64url=spki,
        canonical_device_name="Synthetic Android",
        platform="ANDROID",
        roles=["CLIENT"],
        now=NOW,
    )
    result = service.complete_device_bootstrap(
        session,
        bootstrap_challenge_id=challenge.bootstrap_challenge_id,
        device_signature_b64url=_sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=1),
    )

    assert len(result.memberships) == 2
    assert result.initial_tenant_id is None
    assert _count(session, TenantRow) == 2


def test_existing_device_key_cannot_cross_human_identity(session, service):
    _seed_grant(session)
    private, spki = _device_key()
    challenge = service.start_device_bootstrap(
        session,
        continuation_token=TOKEN,
        public_key_spki_b64url=spki,
        canonical_device_name="Synthetic Android",
        platform="ANDROID",
        roles=["CLIENT"],
        now=NOW,
    )
    bootstrap = session.get(DeviceBootstrapChallengeRow, challenge.bootstrap_challenge_id)
    session.add(HumanIdentityRow(id="hid_" + "z" * 24, created_at=NOW))
    session.add(ClientDeviceRow(
        id="cdev_" + "d" * 24,
        human_identity_id="hid_" + "z" * 24,
        public_key_fingerprint=bootstrap.public_key_fingerprint,
        public_key_spki=bootstrap.public_key_spki,
        canonical_name="Other synthetic device",
        platform="ANDROID",
        roles=["CLIENT"],
        status="ACTIVE",
        created_at=NOW,
        updated_at=NOW,
    ))
    session.flush()

    with pytest.raises(DeviceBootstrapDeviceConflict):
        service.complete_device_bootstrap(
            session,
            bootstrap_challenge_id=challenge.bootstrap_challenge_id,
            device_signature_b64url=_sign(private, challenge.challenge_b64url),
            now=NOW + timedelta(seconds=1),
        )
