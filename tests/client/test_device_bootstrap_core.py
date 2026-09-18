from dataclasses import FrozenInstanceError

import pytest

from attention_router.core.client.bootstrap import (
    ClientDevicePlatform,
    ClientDeviceRole,
    ClientDeviceStatus,
    ClientDeviceView,
    DeviceBootstrapEstablished,
    MembershipStatus,
    TenantMembershipView,
    TenantRole,
)


def test_bootstrap_authority_models_are_immutable_and_provider_neutral():
    membership = TenantMembershipView(
        membership_id="mbr_synthetic_1",
        tenant_id="tnt_synthetic_1",
        role=TenantRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    device = ClientDeviceView(
        device_id="dev_synthetic_1",
        public_key_fingerprint="sha256:" + "a" * 64,
        canonical_name="Synthetic Android",
        platform=ClientDevicePlatform.ANDROID,
        roles=(ClientDeviceRole.CLIENT, ClientDeviceRole.CAPABILITY_NODE),
        status=ClientDeviceStatus.ACTIVE,
    )
    established = DeviceBootstrapEstablished(
        human_identity_id="hid_synthetic_1",
        memberships=(membership,),
        initial_tenant_id="tnt_synthetic_1",
        device=device,
    )

    assert established.status == "DEVICE_BOOTSTRAP_ESTABLISHED"
    assert established.memberships[0].role is TenantRole.OWNER
    assert established.device.platform is ClientDevicePlatform.ANDROID

    for forbidden in ("email", "subject", "provider", "access_token", "refresh_token"):
        assert not hasattr(established, forbidden)
        assert not hasattr(established.device, forbidden)
        assert not hasattr(established.memberships[0], forbidden)

    with pytest.raises(FrozenInstanceError):
        established.initial_tenant_id = "tnt_other"


def test_multiple_memberships_may_have_no_initial_tenant_selection():
    established = DeviceBootstrapEstablished(
        human_identity_id="hid_synthetic_1",
        memberships=(
            TenantMembershipView(
                membership_id="mbr_a",
                tenant_id="tnt_a",
                role=TenantRole.OWNER,
                status=MembershipStatus.ACTIVE,
            ),
            TenantMembershipView(
                membership_id="mbr_b",
                tenant_id="tnt_b",
                role=TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            ),
        ),
        initial_tenant_id=None,
        device=ClientDeviceView(
            device_id="dev_synthetic_1",
            public_key_fingerprint="sha256:" + "b" * 64,
            canonical_name="Synthetic Android",
            platform=ClientDevicePlatform.ANDROID,
            roles=(ClientDeviceRole.CLIENT,),
            status=ClientDeviceStatus.ACTIVE,
        ),
    )

    assert established.initial_tenant_id is None
    assert len(established.memberships) == 2
