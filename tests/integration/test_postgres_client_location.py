"""PostgreSQL authority and latest-snapshot proof for V0.4A."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import base64
import hashlib
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationService,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.config import Settings
from attention_router.core.client.location import CurrentLocationObservation
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_models import (
    ClientLocationSnapshotRow,
)
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow
from attention_router.security.device_keys import encode_unpadded_base64url


pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 18, 23, 45, tzinfo=UTC)
HUMAN_ID = "hid_" + "h" * 24
TENANT_ID = "tnt_" + "t" * 24
DEVICE_ID = "cdev_" + "d" * 24


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


def session_service() -> ClientSessionService:
    return ClientSessionService(settings=settings())


def location_service() -> ClientLocationService:
    return ClientLocationService(
        settings=settings(),
        session_service=session_service(),
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


def seed(Session, spki: bytes):
    with Session.begin() as session:
        session.add(HumanIdentityRow(id=HUMAN_ID, created_at=NOW))
        session.add(
            TenantRow(
                id=TENANT_ID,
                slug="personal-location-postgres",
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
                public_key_fingerprint=(
                    "sha256:" + hashlib.sha256(spki).hexdigest()
                ),
                public_key_spki=spki,
                canonical_name="Physical-like Android",
                platform="ANDROID",
                roles=["CAPABILITY_NODE", "CLIENT"],
                status="ACTIVE",
                created_at=NOW,
                updated_at=NOW,
            )
        )


def issue_session(Session, private, public):
    with Session.begin() as session:
        challenge = session_service().start_session(
            session,
            public_key_spki_b64url=public,
            requested_tenant_id=None,
            now=NOW + timedelta(seconds=1),
        )
    with Session.begin() as session:
        return session_service().complete_session(
            session,
            session_challenge_id=challenge.session_challenge_id,
            device_signature_b64url=sign(
                private,
                challenge.challenge_b64url,
            ),
            now=NOW + timedelta(seconds=2),
        )


def observation(offset_seconds: int, *, latitude: float):
    return CurrentLocationObservation(
        latitude=latitude,
        longitude=-38.54,
        accuracy_m=15.0,
        captured_at=NOW + timedelta(seconds=offset_seconds),
    )


def test_postgres_current_location_is_latest_only_and_authorized(Session):
    private, spki, public = keypair()
    seed(Session, spki)
    issued = issue_session(Session, private, public)

    with Session.begin() as session:
        first = location_service().put_current(
            session,
            session_token=issued.session_token,
            observation=observation(3, latitude=-3.73),
            now=NOW + timedelta(seconds=4),
        )
        first_id = first.location_snapshot_id

    with Session.begin() as session:
        second = location_service().put_current(
            session,
            session_token=issued.session_token,
            observation=observation(5, latitude=-3.731),
            now=NOW + timedelta(seconds=6),
        )
        assert second.location_snapshot_id == first_id

    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(ClientLocationSnapshotRow)
        ) == 1
        row = session.scalar(select(ClientLocationSnapshotRow))
        assert row.latitude == -3.731

        readback = location_service().get_current(
            session,
            session_token=issued.session_token,
            now=NOW + timedelta(seconds=7),
        )
        assert readback.human_identity_id == HUMAN_ID
        assert readback.device_id == DEVICE_ID
        assert readback.tenant_id == TENANT_ID


def test_postgres_membership_revocation_invalidates_location_access(Session):
    private, spki, public = keypair()
    seed(Session, spki)
    issued = issue_session(Session, private, public)

    with Session.begin() as session:
        location_service().put_current(
            session,
            session_token=issued.session_token,
            observation=observation(3, latitude=-3.73),
            now=NOW + timedelta(seconds=4),
        )

    with Session.begin() as session:
        membership = session.scalar(select(ClientTenantMembershipRow))
        membership.status = "SUSPENDED"

    with Session() as session:
        with pytest.raises(ClientLocationAuthorityRejected):
            location_service().get_current(
                session,
                session_token=issued.session_token,
                now=NOW + timedelta(seconds=5),
            )


def test_concurrent_location_writes_converge_on_one_latest_row(Session):
    from attention_router.application.client_location import ClientLocationStale

    private, spki, public = keypair()
    seed(Session, spki)
    issued = issue_session(Session, private, public)
    barrier = threading.Barrier(2)

    def write(item):
        offset_seconds, latitude = item
        with Session.begin() as session:
            barrier.wait()
            try:
                return location_service().put_current(
                    session,
                    session_token=issued.session_token,
                    observation=observation(
                        offset_seconds,
                        latitude=latitude,
                    ),
                    now=NOW + timedelta(seconds=12),
                )
            except ClientLocationStale:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                write,
                [
                    (10, -3.730),
                    (11, -3.731),
                ],
            )
        )

    assert sum(item is not None for item in results) >= 1
    with Session() as session:
        assert session.scalar(
            select(func.count()).select_from(ClientLocationSnapshotRow)
        ) == 1
        row = session.scalar(select(ClientLocationSnapshotRow))
        assert row.latitude == -3.731
        assert row.captured_at.replace(tzinfo=UTC) == (
            NOW + timedelta(seconds=11)
        )
