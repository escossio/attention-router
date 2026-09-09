from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from attention_router.core.control_plane import (
    ApprovalRule,
    AuthorizationRequest,
    AuthorizationState,
    CapabilityDefinition,
    DecisionMode,
    GrantMode,
    PermissionGrant,
)


def test_capability_definition_supports_approval_and_future_grants():
    capability = CapabilityDefinition(
        key="personal.identity.cpf",
        title="CPF",
        description="Disclosure of the represented owner's CPF.",
        kind="DISCLOSURE",
        sensitivity="RESTRICTED",
        grant_modes=["ONE_TIME", "TIME_BOUND", "PERSISTENT"],
    )

    assert capability.default_disposition == DecisionMode.REQUIRE_APPROVAL
    assert GrantMode.PERSISTENT in capability.grant_modes


def test_capability_none_grant_mode_is_exclusive():
    with pytest.raises(ValidationError):
        CapabilityDefinition(
            key="personal.relationship.status",
            title="Relationship status",
            description="Relationship status disclosure.",
            kind="DISCLOSURE",
            sensitivity="PERSONAL",
            grant_modes=["NONE", "PERSISTENT"],
        )


def test_time_bound_rule_requires_ttl():
    with pytest.raises(ValidationError):
        ApprovalRule(
            id="rule-cpf",
            capability_key="personal.identity.cpf",
            grant_on_approval="TIME_BOUND",
        )


def test_non_approval_rule_cannot_issue_grant():
    with pytest.raises(ValidationError):
        ApprovalRule(
            id="rule-public",
            capability_key="profile.public.bio",
            resolution="ALLOW",
            grant_on_approval="PERSISTENT",
        )


def test_permission_grant_requires_real_grant_mode_and_valid_window():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        PermissionGrant(
            id="grant-1",
            capability_key="personal.identity.cpf",
            subject_actor_key="owner",
            grantee_actor_key="contact-1",
            mode="NONE",
            valid_from=now,
            source_authorization_id="authorization-1",
        )

    grant = PermissionGrant(
        id="grant-2",
        capability_key="personal.identity.cpf",
        subject_actor_key="owner",
        grantee_actor_key="contact-1",
        mode="TIME_BOUND",
        valid_from=now,
        expires_at=now + timedelta(hours=1),
        source_authorization_id="authorization-2",
    )
    assert grant.expires_at is not None


def test_only_approved_authorization_can_reference_resulting_grant():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        AuthorizationRequest(
            id="authorization-1",
            capability_key="personal.identity.cpf",
            subject_actor_key="owner",
            requester_actor_key="contact-1",
            state="PENDING",
            requested_at=now,
            expires_at=now + timedelta(minutes=10),
            resulting_grant_id="grant-1",
            correlation_id="correlation-1",
        )

    approved = AuthorizationRequest(
        id="authorization-2",
        capability_key="personal.identity.cpf",
        subject_actor_key="owner",
        requester_actor_key="contact-1",
        state="APPROVED",
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        resulting_grant_id="grant-2",
        correlation_id="correlation-2",
    )
    assert approved.state == AuthorizationState.APPROVED
