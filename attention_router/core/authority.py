from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from attention_router.core.capabilities import CapabilityAvailability


class AuthorityResult(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class EffectiveAuthority:
    result: AuthorityResult
    reason_code: str
    policy_allows: bool
    grant_active: bool


def evaluate_effective_authority(
    *,
    availability: CapabilityAvailability,
    policy_allows: bool,
    grant_active: bool,
    side_effect: bool,
    default_approval_policy: str,
) -> EffectiveAuthority:
    if availability not in {
        CapabilityAvailability.PROVISIONED,
        CapabilityAvailability.SANDBOX_PROVED,
        CapabilityAvailability.OPERATIONAL,
    }:
        return EffectiveAuthority(AuthorityResult.UNAVAILABLE, "CAPABILITY_UNAVAILABLE", policy_allows, grant_active)
    if not policy_allows:
        return EffectiveAuthority(AuthorityResult.DENY, "POLICY_DENIED", False, grant_active)
    if not grant_active:
        return EffectiveAuthority(AuthorityResult.DENY, "CAPABILITY_GRANT_MISSING", True, False)
    if side_effect or default_approval_policy.upper() == "REQUIRES_APPROVAL":
        return EffectiveAuthority(AuthorityResult.REQUIRES_APPROVAL, "APPROVAL_REQUIRED", True, True)
    return EffectiveAuthority(AuthorityResult.ALLOW, "AUTHORIZED", True, True)
