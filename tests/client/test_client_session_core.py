from datetime import UTC, datetime, timedelta

import pytest

from attention_router.core.client.bootstrap import (
    ClientDeviceRole,
    ClientDeviceStatus,
    MembershipStatus,
    TenantRole,
)
from attention_router.core.client.session import (
    ClientSessionAuthorityError,
    ClientSessionGrant,
    ClientSessionState,
    SessionDeviceAuthority,
    SessionMembershipAuthority,
    evaluate_client_session_authority,
    require_client_session_authority,
    resolve_session_tenant,
)


NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def membership(
    *,
    tenant_id: str = "tnt_a",
    status: MembershipStatus = MembershipStatus.ACTIVE,
    human_identity_id: str = "hid_a",
) -> SessionMembershipAuthority:
    return SessionMembershipAuthority(
        membership_id="ctm_a",
        human_identity_id=human_identity_id,
        tenant_id=tenant_id,
        role=TenantRole.OWNER,
        status=status,
    )


def device(
    *,
    status: ClientDeviceStatus = ClientDeviceStatus.ACTIVE,
    human_identity_id: str = "hid_a",
    device_id: str = "cdev_a",
    roles: tuple[ClientDeviceRole, ...] = (ClientDeviceRole.CLIENT,),
) -> SessionDeviceAuthority:
    return SessionDeviceAuthority(
        device_id=device_id,
        human_identity_id=human_identity_id,
        status=status,
        roles=roles,
    )


def client_session(
    *,
    state: ClientSessionState = ClientSessionState.ACTIVE,
    human_identity_id: str = "hid_a",
    device_id: str = "cdev_a",
    tenant_id: str = "tnt_a",
    expires_at=NOW + timedelta(minutes=15),
) -> ClientSessionGrant:
    return ClientSessionGrant(
        session_id="csn_a",
        human_identity_id=human_identity_id,
        device_id=device_id,
        tenant_id=tenant_id,
        state=state,
        expires_at=expires_at,
    )


def test_one_active_membership_is_selected_without_client_tenant_claim():
    assert resolve_session_tenant((membership(),), requested_tenant_id=None) == "tnt_a"


def test_requested_tenant_is_only_accepted_when_it_matches_active_membership():
    memberships = (
        membership(tenant_id="tnt_a"),
        membership(tenant_id="tnt_b"),
    )
    assert resolve_session_tenant(
        memberships,
        requested_tenant_id="tnt_b",
    ) == "tnt_b"
    with pytest.raises(ClientSessionAuthorityError, match="TENANT_FORBIDDEN"):
        resolve_session_tenant(memberships, requested_tenant_id="tnt_attacker")


def test_multiple_memberships_without_selection_fail_closed():
    with pytest.raises(ClientSessionAuthorityError, match="ACTIVE_TENANT_REQUIRED"):
        resolve_session_tenant(
            (membership(tenant_id="tnt_a"), membership(tenant_id="tnt_b")),
            requested_tenant_id=None,
        )


def test_zero_active_memberships_never_fall_back_to_default_tenant():
    with pytest.raises(ClientSessionAuthorityError, match="NO_ACTIVE_MEMBERSHIP"):
        resolve_session_tenant(
            (membership(status=MembershipStatus.SUSPENDED),),
            requested_tenant_id=None,
        )


def test_matching_current_session_authority_is_allowed():
    decision = require_client_session_authority(
        session=client_session(),
        device=device(),
        membership=membership(),
        tenant_active=True,
        now=NOW,
    )
    assert decision.allowed is True
    assert decision.reason_code == "AUTHORIZED"


@pytest.mark.parametrize(
    ("session_value", "device_value", "membership_value", "tenant_active", "now", "reason"),
    [
        (client_session(state=ClientSessionState.REVOKED), device(), membership(), True, NOW, "SESSION_INACTIVE"),
        (client_session(expires_at=NOW), device(), membership(), True, NOW, "SESSION_EXPIRED"),
        (client_session(), device(status=ClientDeviceStatus.REVOKED), membership(), True, NOW, "DEVICE_INACTIVE"),
        (
            client_session(),
            device(roles=(ClientDeviceRole.CAPABILITY_NODE,)),
            membership(),
            True,
            NOW,
            "DEVICE_ROLE_MISSING",
        ),
        (client_session(), device(human_identity_id="hid_b"), membership(), True, NOW, "IDENTITY_MISMATCH"),
        (client_session(), device(device_id="cdev_b"), membership(), True, NOW, "DEVICE_MISMATCH"),
        (
            client_session(),
            device(),
            membership(status=MembershipStatus.SUSPENDED),
            True,
            NOW,
            "MEMBERSHIP_INACTIVE",
        ),
        (client_session(), device(), membership(human_identity_id="hid_b"), True, NOW, "IDENTITY_MISMATCH"),
        (client_session(), device(), membership(tenant_id="tnt_b"), True, NOW, "TENANT_MISMATCH"),
        (client_session(), device(), membership(), False, NOW, "TENANT_INACTIVE"),
        (
            client_session(),
            device(),
            membership(),
            True,
            NOW.replace(tzinfo=None),
            "INVALID_TIME_CONTEXT",
        ),
    ],
)
def test_session_authority_negative_matrix(
    session_value,
    device_value,
    membership_value,
    tenant_active,
    now,
    reason,
):
    decision = evaluate_client_session_authority(
        session=session_value,
        device=device_value,
        membership=membership_value,
        tenant_active=tenant_active,
        now=now,
    )
    assert decision.allowed is False
    assert decision.reason_code == reason
    with pytest.raises(ClientSessionAuthorityError, match=reason):
        require_client_session_authority(
            session=session_value,
            device=device_value,
            membership=membership_value,
            tenant_active=tenant_active,
            now=now,
        )
