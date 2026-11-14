"""No provider calls: mock the existing semantic model boundary."""


import pytest

from attention_router.application import owner_temporal_semantic as semantic
from attention_router.application.owner_temporal_duration import (
    TemporalIntention,
    TemporalProposal,
    TemporalScope,
    TemporalUnit,
)


def fake(quantity=1, unit="YEAR", intention="UNRESOLVED", scope="UNRESOLVED",
         condition="UNRESOLVED"):
    return TemporalProposal.model_validate({
        "quantity": quantity,
        "unit": unit,
        "intention": intention,
        "scope": scope,
        "condition": condition,
    })


def test_plain_year_without_model_does_not_guess_scope(monkeypatch):
    monkeypatch.setattr(semantic, "_run_agent", lambda *_: pytest.fail("no model needed"))
    parsed = semantic.propose_owner_time("Andy, aguarde um ano")
    assert parsed.quantity == 1
    assert parsed.unit == TemporalUnit.YEAR
    assert parsed.intention == TemporalIntention.UNRESOLVED
    assert parsed.scope == TemporalScope.UNRESOLVED


def test_existing_model_not_invoked_when_disabled(monkeypatch):
    monkeypatch.setattr(semantic.settings, "owner_control_semantic_enabled", False)
    monkeypatch.setattr(semantic, "_run_agent", lambda *_: pytest.fail("model disabled"))
    assert semantic.propose_owner_time("daqui a um feriado", use_model=True) is None


def test_ai_proposal_remains_unexecuted(monkeypatch):
    monkeypatch.setattr(semantic.settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(
        semantic, "_run_agent",
        lambda _: semantic.TemporalSemanticOutput(
            classification="PROPOSED",
            proposal=fake(1, "YEAR", "DEFAULT_REPLY_GRACE", "GLOBAL", "NO_OWNER_REPLY"),
        ),
    )
    parsed = semantic.propose_owner_time("Passe a esperar até o próximo ano")
    assert parsed.intention == TemporalIntention.DEFAULT_REPLY_GRACE
    assert parsed.quantity == 1
    assert parsed.unit == TemporalUnit.YEAR


def test_ai_attempt_to_claim_unauthorized_scope_is_rejected(monkeypatch):
    monkeypatch.setattr(semantic.settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(
        semantic, "_run_agent",
        lambda _: semantic.TemporalSemanticOutput(
            classification="PROPOSED",
            proposal=fake(1, "YEAR", "ONE_SHOT_REPLY", "GLOBAL", "UNCONDITIONAL"),
        ),
    )
    with pytest.raises(semantic.TemporalSemanticError, match="PROPOSAL_INVALID"):
        semantic.propose_owner_time("na conversa, aguarde um ano")


def test_questions_do_not_reach_model(monkeypatch):
    monkeypatch.setattr(semantic, "_run_agent", lambda *_: pytest.fail("question"))
    assert semantic.propose_owner_time("Andy, você aguarda um ano?") is None


def test_unresolved_model_is_no_action(monkeypatch):
    monkeypatch.setattr(semantic.settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(semantic, "_run_agent", lambda _: semantic.TemporalSemanticOutput(
        classification="UNRESOLVED",
        proposal=None,
    ))
    assert semantic.propose_owner_time("aguarde alguns dias") is None
