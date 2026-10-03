from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from attention_router.application.client_profile import (
    ClientProfileAuthorityRejected,
    ClientProfileService,
)
from attention_router.config import settings
from attention_router.core.client.bootstrap import (
    MembershipStatus,
    TenantMembershipView,
    TenantRole,
)
from attention_router.infrastructure.human_identity_models import (
    HumanIdentityRow,
    HumanProfileRow,
)


class FakeClientSessions:
    def __init__(self, *, human_id: str, tenant_id: str, role: TenantRole):
        self.human_id = human_id
        self.tenant_id = tenant_id
        self.role = role

    def authenticated_bootstrap(self, _session, *, session_token, now):
        assert session_token == "token"
        return SimpleNamespace(
            human_identity_id=self.human_id,
            active_tenant_id=self.tenant_id,
            memberships=(
                TenantMembershipView(
                    membership_id="membership",
                    tenant_id=self.tenant_id,
                    role=self.role,
                    status=MembershipStatus.ACTIVE,
                ),
            ),
        )


def test_owner_can_configure_assistant_reference_name(session, monkeypatch):
    now = datetime.now(UTC)
    human_id = "hid_profile_owner"
    tenant_id = "00000000-0000-4000-8000-000000000001"
    session.add(HumanIdentityRow(id=human_id, created_at=now))
    session.flush()
    monkeypatch.setattr(settings, "client_profile_enabled", True)
    service = ClientProfileService(
        settings=settings,
        client_sessions=FakeClientSessions(
            human_id=human_id,
            tenant_id=tenant_id,
            role=TenantRole.OWNER,
        ),
    )
    result = service.update(
        session,
        session_token="token",
        assistant_reference_name="  Leonardo   Escossio  ",
        now=now,
    )
    assert result.assistant_reference_name == "Leonardo Escossio"
    stored = session.get(HumanProfileRow, human_id)
    assert stored is not None
    assert stored.assistant_reference_name == "Leonardo Escossio"


def test_non_owner_cannot_change_public_reference_name(session, monkeypatch):
    now = datetime.now(UTC)
    human_id = "hid_profile_member"
    session.add(HumanIdentityRow(id=human_id, created_at=now))
    session.flush()
    monkeypatch.setattr(settings, "client_profile_enabled", True)
    service = ClientProfileService(
        settings=settings,
        client_sessions=FakeClientSessions(
            human_id=human_id,
            tenant_id="00000000-0000-4000-8000-000000000001",
            role=TenantRole.MEMBER,
        ),
    )
    with pytest.raises(ClientProfileAuthorityRejected):
        service.update(
            session,
            session_token="token",
            assistant_reference_name="Leonardo",
            now=now,
        )
    assert session.get(HumanProfileRow, human_id) is None
