"""Exercise the actual, pure authority evaluator with synthetic scenarios.

This is not a full agent, human-approval workflow, or external execution demo.
Run from the repository root: python -m examples.public_authority_demo
"""

from __future__ import annotations

from dataclasses import dataclass

from attention_router.core.authority import EffectiveAuthority, evaluate_effective_authority
from attention_router.core.capabilities import CapabilityAvailability


@dataclass(frozen=True)
class AuthorityScenario:
    name: str
    availability: CapabilityAvailability
    policy_allows: bool
    grant_active: bool
    side_effect: bool
    default_approval_policy: str = "AUTO_ALLOWED"


SCENARIOS = (
    AuthorityScenario("declared_not_ready", CapabilityAvailability.DECLARED, True, True, False),
    AuthorityScenario("policy_rejects", CapabilityAvailability.OPERATIONAL, False, True, False),
    AuthorityScenario("grant_missing", CapabilityAvailability.OPERATIONAL, True, False, False),
    AuthorityScenario("external_write", CapabilityAvailability.OPERATIONAL, True, True, True),
    AuthorityScenario(
        "read_requires_review", CapabilityAvailability.OPERATIONAL, True, True, False,
        "REQUIRES_APPROVAL",
    ),
    AuthorityScenario("read_permitted", CapabilityAvailability.OPERATIONAL, True, True, False),
    AuthorityScenario("capability_revoked", CapabilityAvailability.REVOKED, True, True, False),
)


def evaluate_scenarios() -> list[tuple[AuthorityScenario, EffectiveAuthority]]:
    """Invoke the same core evaluator used by capability resolution; no effects."""
    return [
        (
            scenario,
            evaluate_effective_authority(
                availability=scenario.availability,
                policy_allows=scenario.policy_allows,
                grant_active=scenario.grant_active,
                side_effect=scenario.side_effect,
                default_approval_policy=scenario.default_approval_policy,
            ),
        )
        for scenario in SCENARIOS
    ]


def main() -> None:
    print("REAL AUTHORITY EVALUATOR / SYNTHETIC SCENARIOS")
    print(f"{'SCENARIO':<23} {'DECISION':<19} REASON")
    for scenario, decision in evaluate_scenarios():
        print(f"{scenario.name:<23} {decision.result.value:<19} {decision.reason_code}")
    print("SCOPE: core authority decision only; no agent, database, approval persistence or delivery")


if __name__ == "__main__":
    main()
