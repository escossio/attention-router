from attention_router.domain.models import Policy, PolicyResolution


def _scope_rank(criteria: dict[str, object]) -> int:
    """Rank structural scope before configured priority or fallback defaults."""
    if criteria.get("binding_id"):
        return 400
    if criteria.get("contact_id"):
        return 300
    if criteria.get("audience"):
        return 200
    if criteria.get("relationship_category") or criteria.get("active_context"):
        return 100
    if criteria.get("default") is True:
        return 0
    return -1


def criteria_match_score(
    policy: Policy,
    contact_id: str,
    category: str,
    context: str | None,
    audience: str | None = None,
    binding_id: str | None = None,
) -> int | None:
    criteria = policy.match_criteria
    exact = criteria.get("contact_id")
    binding = criteria.get("binding_id")
    scoped_audience = criteria.get("audience")
    rel = criteria.get("relationship_category")
    ctx = criteria.get("active_context")
    default = criteria.get("default") is True

    score = 0
    if exact:
        if exact != contact_id:
            return None
        score += 100
    if binding:
        if binding != binding_id:
            return None
        score += 200
    if scoped_audience:
        if scoped_audience != audience:
            return None
        score += 50
    if rel:
        if rel != category:
            return None
        score += 30
    if ctx:
        if ctx != context:
            return None
        score += 70
    if default:
        score += 1
    if not (exact or binding or scoped_audience or rel or ctx or default):
        return None
    return score


def resolve_policy(
    policies: list[Policy],
    contact_id: str,
    category: str,
    active_context: str | None,
    audience: str | None = None,
    binding_id: str | None = None,
) -> PolicyResolution:
    matched = []
    for policy in policies:
        score = criteria_match_score(policy, contact_id, category, active_context, audience, binding_id)
        if score is not None:
            matched.append(
                {
                    "policy_id": policy.identifier,
                    "name": policy.name,
                    "match_score": score,
                    "priority": policy.priority,
                    "specificity": policy.specificity,
                    "scope_rank": _scope_rank(policy.match_criteria),
                    "matching_dimensions": sorted(
                        key for key in ("binding_id", "contact_id", "audience", "relationship_category", "active_context", "default")
                        if policy.match_criteria.get(key)
                    ),
                    "tie_breaker": policy.identifier,
                }
            )
    if not matched:
        raise ValueError("no policy matched")

    matched.sort(
        key=lambda item: (
            item["scope_rank"],
            item["priority"],
            item["specificity"],
            item["match_score"],
            item["tie_breaker"],
        ),
        reverse=True,
    )
    winner_id = matched[0]["policy_id"]
    winner = next(policy for policy in policies if policy.identifier == winner_id)
    reason = (
        f"winner={winner_id}; ordered by structural scope, priority, specificity, "
        "match_score, then identifier descending"
    )
    return PolicyResolution(winner=winner, matched=matched, reason=reason)


DEMO_POLICIES = [
    Policy(
        identifier="mae",
        name="Mãe",
        match_criteria={"contact_id": "contact_mae", "relationship_category": "family_core"},
        priority=100,
        specificity=100,
        tone="proximo",
        initial_wait_seconds=2,
        allowed_disclosures=["availability_hint"],
        allowed_actions=["cell_phone", "tv_overlay", "speaker_alert"],
        escalation_steps=["cell_phone", "tv_overlay", "speaker_alert"],
        ack_timeout_seconds=20,
        repetition_limit=2,
        cancellation_conditions=["human_reply"],
        completion_conditions=["acknowledgement"],
    ),
    Policy(
        identifier="pai",
        name="Pai",
        match_criteria={"contact_id": "contact_pai", "relationship_category": "family_core"},
        priority=90,
        specificity=90,
        tone="cordial_brincalhao",
        initial_wait_seconds=4,
        allowed_disclosures=["availability_hint"],
        allowed_actions=["cell_phone", "speaker_alert"],
        escalation_steps=["cell_phone", "speaker_alert"],
        ack_timeout_seconds=25,
        repetition_limit=2,
        cancellation_conditions=["human_reply"],
        completion_conditions=["acknowledgement"],
    ),
    Policy(
        identifier="recrutador",
        name="Recrutador",
        match_criteria={"active_context": "interview"},
        priority=85,
        specificity=80,
        tone="profissional",
        initial_wait_seconds=1,
        allowed_disclosures=["professional_availability_only"],
        allowed_actions=["cell_phone", "tv_overlay"],
        escalation_steps=["cell_phone", "tv_overlay"],
        ack_timeout_seconds=30,
        repetition_limit=1,
        cancellation_conditions=["human_reply"],
        completion_conditions=["acknowledgement", "safe_completion"],
    ),
    Policy(
        identifier="desconhecido",
        name="Desconhecido",
        match_criteria={"default": True},
        priority=1,
        specificity=1,
        tone="neutro_seguro",
        initial_wait_seconds=1,
        allowed_disclosures=[],
        allowed_actions=["soft_ping"],
        escalation_steps=["soft_ping"],
        ack_timeout_seconds=10,
        repetition_limit=1,
        cancellation_conditions=["human_reply"],
        completion_conditions=["safe_completion"],
    ),
]
