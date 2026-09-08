from types import SimpleNamespace

from attention_router.application.platform.disclosure import evaluate_disclosure_authority


def state():
    return [{"namespace": "presence", "value": {"audience_scope": "everyone"}}]


def test_disclosure_allows_only_the_existing_runtime_contract():
    result = evaluate_disclosure_authority(
        directives=[SimpleNamespace(effect_type="DISCLOSE_CURRENT_PRESENCE")],
        allowed_disclosures=["availability_hint"],
        operational_state=state(),
    )
    assert result.allowed is True


def test_disclosure_fails_closed_without_directive_policy_or_presence():
    directive = [SimpleNamespace(effect_type="DISCLOSE_CURRENT_PRESENCE")]
    assert not evaluate_disclosure_authority(
        directives=[], allowed_disclosures=["availability_hint"], operational_state=state()
    ).allowed
    assert not evaluate_disclosure_authority(
        directives=directive, allowed_disclosures=[], operational_state=state()
    ).allowed
    assert not evaluate_disclosure_authority(
        directives=directive, allowed_disclosures=["availability_hint"], operational_state=[]
    ).allowed
