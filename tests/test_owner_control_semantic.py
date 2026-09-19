import pytest
from pydantic import ValidationError
from sqlalchemy import select

from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
    parse_owner_grace_control,
)
from attention_router.application import owner_control_semantic, services
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    GraceSecondsParameters,
    OwnerControlAction,
)
from attention_router.application.owner_control_semantic import (
    OwnerControlSemanticOutput,
    interpret_owner_control_semantically,
    normalize_semantic_output,
)
from attention_router.config import settings
from attention_router.infrastructure.models import (
    OutboxMessageRow,
    OwnerOperationalControlRow,
)
from tests.test_owner_control import _install_owner_and_grace, _owner_event


@pytest.fixture
def owner_control_session(session):
    _install_owner_and_grace(session)
    return session


def semantic_output(**overrides):
    values = {
        "classification": "MATCHED",
        "action": "SET_OWNER_REPLY_GRACE_SECONDS",
        "confidence": "high",
        "seconds": 30,
        "enabled": None,
        "reference": None,
    }
    values.update(overrides)
    return OwnerControlSemanticOutput(**values)


def test_semantic_delay_normalizes_to_existing_typed_command():
    parsed = normalize_semantic_output(semantic_output())
    assert parsed.status == OwnerControlParseStatus.MATCHED
    assert parsed.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS
    assert parsed.parameters == GraceSecondsParameters(seconds=30)


def test_semantic_resume_normalizes_to_existing_typed_command():
    parsed = normalize_semantic_output(
        semantic_output(
            action="SET_AUTOMATIC_RESPONSES_ENABLED",
            seconds=None,
            enabled=True,
        )
    )
    assert parsed.status == OwnerControlParseStatus.MATCHED
    assert parsed.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
    assert parsed.parameters == AutomaticResponsesEnabledParameters(enabled=True)


@pytest.mark.parametrize(
    ("classification", "confidence", "expected_status", "reason"),
    [
        ("UNRESOLVED", "high", OwnerControlParseStatus.NOT_CONTROL_COMMAND, None),
        (
            "AMBIGUOUS",
            "high",
            OwnerControlParseStatus.REJECTED,
            "CONTROL_COMMAND_NEEDS_CLARIFICATION",
        ),
        (
            "MATCHED",
            "medium",
            OwnerControlParseStatus.REJECTED,
            "CONTROL_COMMAND_NEEDS_CLARIFICATION",
        ),
    ],
)
def test_semantic_uncertainty_never_executes(
    classification,
    confidence,
    expected_status,
    reason,
):
    output = semantic_output(
        classification=classification,
        confidence=confidence,
        action=None if classification != "MATCHED" else "SET_OWNER_REPLY_GRACE_SECONDS",
        seconds=None if classification != "MATCHED" else 30,
    )
    parsed = normalize_semantic_output(output)
    assert parsed.status == expected_status
    assert parsed.reason_code == reason


def test_semantic_output_rejects_model_invented_action():
    with pytest.raises(ValidationError):
        OwnerControlSemanticOutput.model_validate(
            {
                "classification": "MATCHED",
                "action": "RUN_SHELL",
                "confidence": "high",
                "seconds": None,
                "enabled": None,
                "reference": None,
            }
        )


def test_semantic_action_parameter_mismatch_fails_closed():
    parsed = normalize_semantic_output(
        semantic_output(
            action="SET_AUTOMATIC_RESPONSES_ENABLED",
            seconds=30,
            enabled=True,
        )
    )
    assert parsed.status == OwnerControlParseStatus.REJECTED
    assert parsed.reason_code == "CONTROL_COMMAND_SEMANTIC_INVALID"


def test_disabled_semantic_interpreter_never_calls_model(monkeypatch):
    monkeypatch.setattr(settings, "owner_control_semantic_enabled", False)
    monkeypatch.setattr(
        owner_control_semantic,
        "_run_model",
        lambda _text: pytest.fail("disabled interpreter must not invoke the model"),
    )
    parsed = interpret_owner_control_semantically("retorne em 30 segundos")
    assert parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND


def test_issue_82_example_reaches_existing_executor(owner_control_session, monkeypatch):
    session = owner_control_session
    assert (
        parse_owner_grace_control("retorne em 30 segundos").status
        == OwnerControlParseStatus.NOT_CONTROL_COMMAND
    )
    monkeypatch.setattr(settings, "owner_control_semantic_enabled", True)
    semantic = OwnerControlParseResult(
        OwnerControlParseStatus.MATCHED,
        OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS,
        GraceSecondsParameters(seconds=30),
    )
    monkeypatch.setattr(
        services,
        "interpret_owner_control_semantically",
        lambda _text: semantic,
    )

    result = services.receive_normalized_inbound_event(
        session,
        _owner_event("semantic-delay-30", "retorne em 30 segundos"),
    )

    outbox = session.scalar(
        select(OutboxMessageRow).where(OutboxMessageRow.interaction_id == result["id"])
    )
    control = session.scalar(select(OwnerOperationalControlRow))
    assert control is not None
    assert control.integer_value == 30
    assert outbox.payload["text"] == "Espera alterada para 30 segundos."


def test_unrelated_owner_text_remains_on_normal_message_path(owner_control_session, monkeypatch):
    monkeypatch.setattr(settings, "owner_control_semantic_enabled", True)
    monkeypatch.setattr(
        services,
        "interpret_owner_control_semantically",
        lambda _text: OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND),
    )

    result = services.receive_normalized_inbound_event(
        owner_control_session,
        _owner_event("semantic-unrelated", "hoje foi um dia puxado"),
    )

    assert result["event_type"] != "OWNER_CONTROL_COMMAND"
    assert owner_control_session.scalar(select(OwnerOperationalControlRow)) is None
