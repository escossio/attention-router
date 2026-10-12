"""Regression checks for a public example using the real authority evaluator."""

from examples.public_authority_demo import evaluate_scenarios, main
from attention_router.core.authority import AuthorityResult


EXPECTED = {
    "declared_not_ready": (AuthorityResult.UNAVAILABLE, "CAPABILITY_UNAVAILABLE"),
    "policy_rejects": (AuthorityResult.DENY, "POLICY_DENIED"),
    "grant_missing": (AuthorityResult.DENY, "CAPABILITY_GRANT_MISSING"),
    "external_write": (AuthorityResult.REQUIRES_APPROVAL, "APPROVAL_REQUIRED"),
    "read_requires_review": (AuthorityResult.REQUIRES_APPROVAL, "APPROVAL_REQUIRED"),
    "read_permitted": (AuthorityResult.ALLOW, "AUTHORIZED"),
    "capability_revoked": (AuthorityResult.UNAVAILABLE, "CAPABILITY_UNAVAILABLE"),
}


def test_all_cases_call_actual_authority_evaluator():
    results = evaluate_scenarios()
    actual = {scenario.name: (decision.result, decision.reason_code) for scenario, decision in results}
    assert actual == EXPECTED
    assert any(s.side_effect and d.result is AuthorityResult.REQUIRES_APPROVAL for s, d in results)
    assert any(not s.side_effect and d.result is AuthorityResult.ALLOW for s, d in results)


def test_cli_labels_demo_scope(capsys):
    main()
    output = capsys.readouterr().out
    assert "REAL AUTHORITY EVALUATOR / SYNTHETIC SCENARIOS" in output
    assert "external_write" in output
    assert "APPROVAL_REQUIRED" in output
    assert "no agent, database, approval persistence or delivery" in output
