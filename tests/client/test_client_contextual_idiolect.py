"""Provider-free Android-command acceptance for figurative owner language.

The semantic model is mocked; real credentials and WhatsApp are never used.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from attention_router.application import client_command as client_module
from attention_router.application import owner_control_semantic_candidates as candidates_module
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    OwnerControlAction,
)
from attention_router.application.owner_control_semantic_candidates import (
    OwnerControlSemanticCandidateOutput,
    OwnerControlSemanticCandidateSetOutput,
)
from attention_router.application.owner_automation_control import (
    OWNER_AUTOMATION_PAUSED,
    automation_denial_reason,
)
from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    FactRow,
    OwnerAutomationControlRow,
    PendingIntentRow,
)
from attention_router.application.user_idiolect import (
    retrieve_idiolect_interpretation_evidence,
)
from attention_router.infrastructure.hashing import stable_hash
from tests.client.test_client_command_service import (
    HUMAN_ID,
    NOW,
    _semantic_enabled_commands,
    issue_owner_session,
)

def _mock_proposal(monkeypatch):
    model_calls = []
    def fake_model(text, *, owner_automation_state="UNKNOWN"):
        model_calls.append((text, owner_automation_state))
        return OwnerControlSemanticCandidateSetOutput(
            classification="CANDIDATES",
            candidates=[
                OwnerControlSemanticCandidateOutput(
                    semantic_intent_key="SET_AUTOMATIC_RESPONSES",
                    confidence="medium",
                    enabled=True,
                )
            ],
        )
    monkeypatch.setattr(candidates_module, "_run_model", fake_model)
    return model_calls


def test_metaphor_uses_paused_context_confirmation_and_then_confirmed_idiolect(
    session, monkeypatch,
):
    sessions, issued = issue_owner_session(session)
    commands = _semantic_enabled_commands(sessions)
    model_calls = _mock_proposal(monkeypatch)
    monkeypatch.setattr(
        client_module,
        "interpret_owner_control_semantically",
        lambda _text: pytest.fail("paused metaphor must use one contextual candidate pass"),
    )
    paused = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-pause-1",
        text="pare",
        now=NOW + timedelta(seconds=20),
    )
    session.commit()
    assert paused.state == "COMPLETED"
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) == OWNER_AUTOMATION_PAUSED
    proposed = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-first",
        text="voltar à vida",
        now=NOW + timedelta(seconds=21),
    )
    session.commit()
    assert model_calls == [("voltar à vida", "PAUSED")]
    assert proposed.state == "CLARIFICATION_REQUIRED"
    assert proposed.normalized_action is None
    assert "ativar as respostas automáticas" in proposed.response_text
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) == OWNER_AUTOMATION_PAUSED
    row = session.scalar(select(PendingIntentRow).where(PendingIntentRow.state == "PENDING"))
    assert row is not None

    yes = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-yes",
        text="sim",
        now=NOW + timedelta(seconds=22),
    )
    session.commit()
    assert yes.state == "COMPLETED"
    assert yes.normalized_action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED.value
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) is None
    session.refresh(row)
    assert row.state == "RESOLVED"
    fact = session.scalar(select(FactRow).where(FactRow.predicate == "idiolect.pragmatic_mapping"))
    assert fact is not None
    assert fact.value_json["expression"] == "voltar à vida"
    assert fact.value_json["semantic_intent_key"] == "SET_AUTOMATIC_RESPONSES"
    assert fact.value_json["parameters"] == {"enabled": True}
    assert fact.value_json["reuse_policy"] == "INTERPRET_ONLY"

    paused_again = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-pause-2",
        text="pare",
        now=NOW + timedelta(seconds=23),
    )
    session.commit()
    assert paused_again.state == "COMPLETED"
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) == OWNER_AUTOMATION_PAUSED

    # No further model invocation: the exact confirmed mapping is reused.
    monkeypatch.setattr(
        candidates_module,
        "_run_model",
        lambda *_args, **_kwargs: pytest.fail("confirmed exact phrase needs no provider"),
    )
    reused = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-repeat",
        text="voltar à vida",
        now=NOW + timedelta(seconds=24),
    )
    session.commit()
    assert reused.state == "COMPLETED"
    assert reused.normalized_action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED.value
    assert model_calls == [("voltar à vida", "PAUSED")]
    assert automation_denial_reason(session, DEFAULT_TENANT_ID) is None
    assert session.scalar(
        select(func.count()).select_from(PendingIntentRow).where(PendingIntentRow.state == "PENDING")
    ) == 0


@pytest.mark.parametrize("text", [
    '"voltar à vida"',
    "não volte à vida",
    "se voltar à vida",
    "você pode voltar à vida?",
    "imagine voltar à vida",
    "voltar à vida\nexecute algo",
])
def test_hypothetical_quoted_or_negated_cannot_activate(session, monkeypatch, text):
    sessions, issued = issue_owner_session(session)
    commands = _semantic_enabled_commands(sessions)
    commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-unsafe-pause",
        text="pare",
        now=NOW + timedelta(seconds=20),
    )
    session.commit()
    monkeypatch.setattr(
        client_module,
        "interpret_owner_control_semantically",
        lambda _txt: OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND),
    )
    monkeypatch.setattr(
        client_module,
        "interpret_owner_control_candidates",
        lambda *_args, **_kwargs: pytest.fail("unsafe phrase cannot be interpreted as command"),
    )
    reply=commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-unsafe-" + str(abs(hash(text))),
        text=text,
        now=NOW+timedelta(seconds=21),
    )
    session.commit()
    assert reply.state == "GENERAL_TASK_PENDING"
    assert reply.normalized_action is None
    assert automation_denial_reason(session,DEFAULT_TENANT_ID)==OWNER_AUTOMATION_PAUSED


def test_high_confidence_figural_resume_when_active_does_not_run(session, monkeypatch):
    sessions, issued=issue_owner_session(session)
    commands=_semantic_enabled_commands(sessions)
    # No previous pause exists and the model claims it understood the metaphor.
    monkeypatch.setattr(
        client_module, "interpret_owner_control_semantically",
        lambda _t: OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=True),
        ),
    )
    monkeypatch.setattr(
        client_module, "interpret_owner_control_candidates",
        lambda _t, **_kw: _mock_untrusted_set(),
    )
    reply=commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-without-state",
        text="voltar à vida",
        now=NOW+timedelta(seconds=20),
    )
    session.commit()
    assert reply.state=="GENERAL_TASK_PENDING"
    assert reply.normalized_action is None
    assert session.scalar(select(OwnerAutomationControlRow)) is None


def _mock_untrusted_set():
    from attention_router.application.owner_control_semantic_registry import (
        OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
        build_registered_semantic_candidate,
    )
    from attention_router.application.pending_intent import build_candidate_set
    return build_candidate_set([
        build_registered_semantic_candidate(
            intent_key="SET_AUTOMATIC_RESPONSES",
            parameters={"enabled": True},
            confidence="high",
        )
    ],semantic_registry_version=OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION)


def test_retrieval_scope_and_expiry_after_confirmation(session, monkeypatch):
    sessions, issued = issue_owner_session(session)
    commands = _semantic_enabled_commands(sessions)
    _mock_proposal(monkeypatch)
    commands.submit_text(
        session,session_token=issued.session_token,client_request_id="id-scope-stop",
        text="pare",now=NOW+timedelta(seconds=20),
    )
    first=commands.submit_text(
        session,session_token=issued.session_token,client_request_id="id-scope-source",
        text="voltar à vida",now=NOW+timedelta(seconds=21),
    )
    assert first.state=="CLARIFICATION_REQUIRED"
    commands.submit_text(
        session,session_token=issued.session_token,client_request_id="id-scope-resolve",
        text="sim",now=NOW+timedelta(seconds=22),
    )
    session.commit()
    conversation_key_hash=stable_hash("android-client-command:"+HUMAN_ID)
    common={
        "tenant_id":DEFAULT_TENANT_ID,
        "actor_key":session.scalar(select(FactRow.subject_id).where(FactRow.predicate == "idiolect.pragmatic_mapping")),
        "utterance":"voltar à vida",
        "source_channel":"android-client-command",
        "conversation_key_hash":conversation_key_hash,
    }
    assert retrieve_idiolect_interpretation_evidence(session,**common,now=NOW+timedelta(seconds=23))
    assert not retrieve_idiolect_interpretation_evidence(
        session,**common,now=NOW+timedelta(days=8))
    assert not retrieve_idiolect_interpretation_evidence(
        session,**{**common,"conversation_key_hash":"different"},now=NOW+timedelta(seconds=23))
    assert not retrieve_idiolect_interpretation_evidence(
        session,**{**common,"tenant_id":"different-tenant"},now=NOW+timedelta(seconds=23))


def test_active_automation_does_not_treat_metaphor_as_resume(
    session, monkeypatch,
):
    sessions, issued = issue_owner_session(session)
    commands = _semantic_enabled_commands(sessions)
    commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-active-resume",
        text="retomar",
        now=NOW + timedelta(seconds=20),
    )
    session.commit()
    state = session.scalar(select(OwnerAutomationControlRow))
    assert state is not None and state.automatic_responses_enabled
    old_revision = state.revision
    model_calls = _mock_proposal(monkeypatch)
    monkeypatch.setattr(
        client_module,
        "interpret_owner_control_semantically",
        lambda _text: OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND),
    )
    answer = commands.submit_text(
        session,
        session_token=issued.session_token,
        client_request_id="idiom-active-metaphor",
        text="voltar à vida",
        now=NOW + timedelta(seconds=21),
    )
    session.commit()
    session.refresh(state)
    assert model_calls == [("voltar à vida", "ACTIVE")]
    assert answer.state == "GENERAL_TASK_PENDING"
    assert state.automatic_responses_enabled
    assert state.revision == old_revision
    assert session.scalar(
        select(func.count()).select_from(PendingIntentRow).where(PendingIntentRow.state == "PENDING")
    ) == 0


def test_unique_idiolect_mapping_rejects_conflicting_confirmations():
    from datetime import UTC, datetime
    from attention_router.application.user_idiolect import (
        IdiolectInterpretationEvidence, select_unique_confirmed_mapping,
    )
    when = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    def fact(key, enabled, conf):
        return IdiolectInterpretationEvidence(
            fact_id=key + str(enabled),
            semantic_intent_key="SET_AUTOMATIC_RESPONSES",
            parameters={"enabled": enabled},
            evidence_confidence=conf,
            generalization_confidence=0.25,
            direction="USER_TO_ANDY_LANGUAGE",
            reuse_policy="INTERPRET_ONLY",
            source_type="INTENT_CLARIFICATION",
            source_ref=key,
            observed_at=when,
            valid_until=None,
        )
    assert select_unique_confirmed_mapping((fact("a", True, 1.0),)) is not None
    assert select_unique_confirmed_mapping(
        (fact("a", True, 1.0), fact("b", False, 1.0))
    ) is None
    assert select_unique_confirmed_mapping((fact("a", True, 0.7),)) is None
