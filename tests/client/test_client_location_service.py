from datetime import UTC, datetime, timedelta
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationService,
    ClientLocationStale,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.config import Settings
from attention_router.core.client.location import (
    ClientLocationPrecision,
    CurrentLocationObservation,
)
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
)
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.security.device_keys import encode_unpadded_base64url


NOW = datetime(2026, 9, 18, 23, 0, tzinfo=UTC)
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
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="x" * 32,
        client_session_enabled=True,
        client_session_challenge_ttl_seconds=300,
        client_session_ttl_seconds=900,
        client_location_enabled=True,
    )


def keypair():
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, spki, encode_unpadded_base64url(spki)


def sign(private, challenge: str) -> str:
    raw = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    return encode_unpadded_base64url(
        private.sign(raw, ec.ECDSA(hashes.SHA256()))
    )


def seed(session, spki: bytes):
    session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
    session.add(
        TenantRow(
            id=TENANT_ID,
            slug="personal-location-proof",
            name="Personal",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    session.add(
        ClientTenantMembershipRow(
            id="ctm_" + "m" * 24,
            human_identity_id=HUMAN_ID,
            tenant_id=TENANT_ID,
            role="OWNER",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        ClientDeviceRow(
            id=DEVICE_ID,
            human_identity_id=HUMAN_ID,
            public_key_fingerprint="sha256:" + hashlib.sha256(spki).hexdigest(),
            public_key_spki=spki,
            canonical_name="Synthetic Android",
            platform="ANDROID",
            roles=["CAPABILITY_NODE", "CLIENT"],
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()


def issue_session(session):
    private, spki, public = keypair()
    seed(session, spki)
    session_service = ClientSessionService(settings=settings())
    challenge = session_service.start_session(
        session,
        public_key_spki_b64url=public,
        requested_tenant_id=None,
        now=NOW + timedelta(seconds=1),
    )
    issued = session_service.complete_session(
        session,
        session_challenge_id=challenge.session_challenge_id,
        device_signature_b64url=sign(private, challenge.challenge_b64url),
        now=NOW + timedelta(seconds=2),
    )
    session.commit()
    return session_service, issued


def location_service(session_service):
    return ClientLocationService(
        settings=settings(),
        session_service=session_service,
    )


def observation(
    *,
    captured_at=NOW + timedelta(seconds=3),
    latitude=-3.73,
    longitude=-38.54,
):
    return CurrentLocationObservation(
        latitude=latitude,
        longitude=longitude,
        accuracy_m=12.5,
        captured_at=captured_at,
        precision=ClientLocationPrecision.PRECISE,
    )


def test_put_and_get_current_location_use_session_authority(session):
    session_service, issued = issue_session(session)
    service = location_service(session_service)

    written = service.put_current(
        session,
        session_token=issued.session_token,
        observation=observation(),
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    assert written.human_identity_id == HUMAN_ID
    assert written.device_id == DEVICE_ID
    assert written.tenant_id == TENANT_ID
    assert written.latitude == -3.73
    assert written.longitude == -38.54
    assert written.precision == "PRECISE"

    readback = service.get_current(
        session,
        session_token=issued.session_token,
        now=NOW + timedelta(seconds=5),
    )
    assert readback.location_snapshot_id == written.location_snapshot_id
    assert readback.device_id == DEVICE_ID
    assert session.scalar(
        select(func.count()).select_from(ClientLocationSnapshotRow)
    ) == 1


def test_newer_observation_updates_same_current_row(session):
    session_service, issued = issue_session(session)
    service = location_service(session_service)
    first = service.put_current(
        session,
        session_token=issued.session_token,
        observation=observation(),
        now=NOW + timedelta(seconds=4),
    )
    session.commit()

    second = service.put_current(
        session,
        session_token=issued.session_token,
        observation=observation(
            captured_at=NOW + timedelta(seconds=6),
            latitude=-3.731,
            longitude=-38.541,
        ),
        now=NOW + timedelta(seconds=7),
    )
    session.commit()

    assert second.location_snapshot_id == first.location_snapshot_id
    assert second.latitude == -3.731
    assert second.longitude == -38.541
    assert session.scalar(
        select(func.count()).select_from(ClientLocationSnapshotRow)
    ) == 1


def test_older_observation_cannot_replace_current_snapshot(session):
    session_service, issued = issue_session(session)
    service = location_service(session_service)
    service.put_current(
        session,
        session_token=issued.session_token,
        observation=observation(captured_at=NOW + timedelta(seconds=8)),
        now=NOW + timedelta(seconds=9),
    )
    session.commit()

    with pytest.raises(ClientLocationStale):
        service.put_current(
            session,
            session_token=issued.session_token,
            observation=observation(captured_at=NOW + timedelta(seconds=7)),
            now=NOW + timedelta(seconds=10),
        )


def test_membership_revocation_blocks_location_access(session):
    session_service, issued = issue_session(session)
    service = location_service(session_service)
    membership = session.scalar(select(ClientTenantMembershipRow))
    membership.status = "SUSPENDED"
    session.commit()

    with pytest.raises(ClientLocationAuthorityRejected):
        service.put_current(
            session,
            session_token=issued.session_token,
            observation=observation(),
            now=NOW + timedelta(seconds=4),
        )
