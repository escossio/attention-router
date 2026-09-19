from __future__ import annotations

from datetime import timedelta

from attention_router.application.platform.capability_pack import execute_owner_capability
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.client_bootstrap_models import (
    ClientDeviceRow,
    ClientTenantMembershipRow,
)
from attention_router.infrastructure.client_location_models import ClientLocationSnapshotRow
from attention_router.infrastructure.human_identity_models import HumanIdentityRow
from attention_router.infrastructure.models import TenantRow


OWNER_ACTOR = "owner-v04b"


def _ensure_tenant(session, tenant_id: str = DEFAULT_TENANT_ID, *, status: str = "ACTIVE"):
    row = session.get(TenantRow, tenant_id)
    if row is None:
        stamp = now_utc()
        row = TenantRow(
            id=tenant_id,
            slug=f"tenant-{tenant_id[-8:]}",
            name="V0.4B Tenant",
            status=status,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        session.flush()
    else:
        row.status = status
    return row


def _seed_owner(
    session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    human_id: str = "hid_v04b_owner",
    membership_status: str = "ACTIVE",
):
    _ensure_tenant(session, tenant_id)
    if session.get(HumanIdentityRow, human_id) is None:
        session.add(HumanIdentityRow(id=human_id, created_at=now_utc()))
        session.flush()
    membership = ClientTenantMembershipRow(
        id=f"ctm_{tenant_id[-8:]}_{human_id[-8:]}",
        human_identity_id=human_id,
        tenant_id=tenant_id,
        role="OWNER",
        status=membership_status,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(membership)
    session.flush()
    return human_id


def _seed_device(
    session,
    *,
    human_id: str,
    device_id: str,
    status: str = "ACTIVE",
):
    row = ClientDeviceRow(
        id=device_id,
        human_identity_id=human_id,
        public_key_fingerprint=("sha256:" + device_id).ljust(71, "x")[:71],
        public_key_spki=("spki-" + device_id).encode(),
        canonical_name=device_id,
        platform="ANDROID",
        roles=["OWNER"],
        status=status,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row


def _seed_snapshot(
    session,
    *,
    tenant_id: str,
    human_id: str,
    device_id: str,
    precision: str = "PRECISE",
    age_seconds: int = 5,
    latitude: float = -3.7623933333333337,
):
    captured = now_utc() - timedelta(seconds=age_seconds)
    row = ClientLocationSnapshotRow(
        id=f"cloc_{device_id}",
        human_identity_id=human_id,
        device_id=device_id,
        tenant_id=tenant_id,
        latitude=latitude,
        longitude=-38.587788333333336,
        accuracy_m=8.1,
        precision=precision,
        captured_at=captured,
        received_at=captured + timedelta(seconds=1),
    )
    session.add(row)
    session.flush()
    return row


def _run(session, tenant_id: str = DEFAULT_TENANT_ID):
    return execute_owner_capability(
        session,
        tenant_id=tenant_id,
        actor_id=OWNER_ACTOR,
        request=CapabilityRequest(
            capability="location.current",
            parameters={},
            user_requested=True,
            confidence="high",
        ),
        correlation_id="v04b-location-provider-test",
    )


def test_current_location_reads_single_authorized_owner_snapshot(session):
    human_id = _seed_owner(session)
    device = _seed_device(session, human_id=human_id, device_id="cdev_v04b_primary")
    snapshot = _seed_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        human_id=human_id,
        device_id=device.id,
    )
    before = (
        snapshot.latitude,
        snapshot.longitude,
        snapshot.accuracy_m,
        snapshot.precision,
        snapshot.captured_at,
        snapshot.received_at,
    )

    result = _run(session)

    assert result.status == "EXECUTED"
    assert result.reason_code == "LOCATION_CURRENT_RESOLVED"
    assert result.result["latitude"] == snapshot.latitude
    assert result.result["longitude"] == snapshot.longitude
    assert result.result["accuracy_m"] == snapshot.accuracy_m
    assert result.result["precision"] == "PRECISE"
    assert result.result["freshness_state"] == "CURRENT"
    assert result.result["age_seconds"] >= 0

    session.refresh(snapshot)
    assert (
        snapshot.latitude,
        snapshot.longitude,
        snapshot.accuracy_m,
        snapshot.precision,
        snapshot.captured_at,
        snapshot.received_at,
    ) == before


def test_current_location_without_snapshot_fails_closed(session):
    human_id = _seed_owner(session)
    _seed_device(session, human_id=human_id, device_id="cdev_v04b_empty")

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_SNAPSHOT_UNAVAILABLE"


def test_foreign_tenant_snapshot_is_not_visible(session):
    human_id = _seed_owner(session)
    _seed_device(session, human_id=human_id, device_id="cdev_v04b_foreign")
    foreign_tenant = "tnt_v04b_foreign"
    _ensure_tenant(session, foreign_tenant)
    session.add(
        ClientTenantMembershipRow(
            id="ctm_v04b_foreign",
            human_identity_id=human_id,
            tenant_id=foreign_tenant,
            role="OWNER",
            status="ACTIVE",
            created_at=now_utc(),
            updated_at=now_utc(),
        )
    )
    session.flush()
    _seed_snapshot(
        session,
        tenant_id=foreign_tenant,
        human_id=human_id,
        device_id="cdev_v04b_foreign",
    )

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_SNAPSHOT_UNAVAILABLE"


def test_inactive_owner_membership_denies_location(session):
    human_id = _seed_owner(session, membership_status="SUSPENDED")
    _seed_device(session, human_id=human_id, device_id="cdev_v04b_member_off")
    _seed_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        human_id=human_id,
        device_id="cdev_v04b_member_off",
    )

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_OWNER_UNAVAILABLE"


def test_revoked_device_denies_snapshot(session):
    human_id = _seed_owner(session)
    _seed_device(
        session,
        human_id=human_id,
        device_id="cdev_v04b_revoked",
        status="REVOKED",
    )
    _seed_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        human_id=human_id,
        device_id="cdev_v04b_revoked",
    )

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_DEVICE_INACTIVE"


def test_multiple_active_devices_with_snapshots_fail_ambiguous(session):
    human_id = _seed_owner(session)
    for suffix, latitude in (("a", -3.70), ("b", -3.80)):
        device_id = f"cdev_v04b_{suffix}"
        _seed_device(session, human_id=human_id, device_id=device_id)
        _seed_snapshot(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            human_id=human_id,
            device_id=device_id,
            latitude=latitude,
        )

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_DEVICE_AMBIGUOUS"


def test_approximate_and_stale_metadata_are_preserved(session):
    human_id = _seed_owner(session)
    _seed_device(session, human_id=human_id, device_id="cdev_v04b_stale")
    _seed_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        human_id=human_id,
        device_id="cdev_v04b_stale",
        precision="APPROXIMATE",
        age_seconds=700,
    )

    result = _run(session)

    assert result.status == "EXECUTED"
    assert result.result["precision"] == "APPROXIMATE"
    assert result.result["freshness_state"] == "STALE"
    assert result.result["age_seconds"] >= 600


def test_inactive_tenant_denies_location(session):
    human_id = _seed_owner(session)
    _seed_device(session, human_id=human_id, device_id="cdev_v04b_tenant_off")
    _seed_snapshot(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        human_id=human_id,
        device_id="cdev_v04b_tenant_off",
    )
    _ensure_tenant(session, DEFAULT_TENANT_ID, status="SUSPENDED")

    result = _run(session)

    assert result.status == "FAILED"
    assert result.reason_code == "LOCATION_TENANT_INACTIVE"
