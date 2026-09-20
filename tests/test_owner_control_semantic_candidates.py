import pytest
from pydantic import ValidationError

from attention_router.application import owner_control_semantic_candidates
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    GraceSecondsParameters,
    OwnerControlAction,
    ReviewReferenceParameters,
)
from attention_router.application.owner_control_semantic_candidates import (
    OwnerControlCandidateBuilderError,
    OwnerControlSemanticCandidateOutput,
    OwnerControlSemanticCandidateSetOutput,
    candidate_set_from_semantic_output,
    interpret_owner_control_candidates,
    normalize_candidate_output,
)
from attention_router.application.owner_control_semantic_registry import (
    OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    OwnerSemanticIntentKey,
    OwnerSemanticRegistryError,
    build_registered_semantic_candidate,
    materialize_owner_control_candidate,
    normalize_semantic_parameters,
    semantic_intent_definition,
    semantic_intent_definitions,
)
from attention_router.application.pending_intent import candidate_set_fingerprint


def raw_candidate(intent, *, confidence="high", seconds=None, enabled=None, reference=None):
    return OwnerControlSemanticCandidateOutput(
        semantic_intent_key=intent,
        confidence=confidence,
        seconds=seconds,
        enabled=enabled,
        reference=reference,
    )


def output(*candidates, classification="CANDIDATES"):
    return OwnerControlSemanticCandidateSetOutput(
        classification=classification,
        candidates=list(candidates),
    )


def test_registry_is_closed_versioned_and_contains_unavailable_meaning():
    definitions = semantic_intent_definitions()
    assert OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION == "owner-control-semantic-v2"
    assert {definition.key for definition in definitions} == set(OwnerSemanticIntentKey)

    one_shot = semantic_intent_definition(OwnerSemanticIntentKey.ONE_SHOT_REPLY_DELAY)
    assert one_shot.capability_status == "UNAVAILABLE"
    assert one_shot.capability_key is None
    assert one_shot.owner_control_action is None

    with pytest.raises(OwnerSemanticRegistryError, match="UNREGISTERED"):
        semantic_intent_definition("RUN_SHELL")


def test_registered_candidate_is_deterministic_and_pending_intent_compatible():
    first = build_registered_semantic_candidate(
        intent_key=OwnerSemanticIntentKey.CONFIGURE_OWNER_REPLY_GRACE,
        parameters={"seconds": 30},
        confidence="high",
    )
    second = build_registered_semantic_candidate(
        intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
        confidence="high",
    )
    assert first == second
    assert first["semantic_intent_key"] == "CONFIGURE_OWNER_REPLY_GRACE"
    assert first["capability_mapping"] == {
        "status": "AVAILABLE",
        "capability_key": "owner_control:SET_OWNER_REPLY_GRACE_SECONDS",
    }
    assert first["candidate_key"].startswith("semantic-")


@pytest.mark.parametrize(
    ("intent", "parameters", "reason"),
    [
        ("CONFIGURE_OWNER_REPLY_GRACE", {"seconds": True}, "SECONDS_INVALID"),
        ("CONFIGURE_OWNER_REPLY_GRACE", {"seconds": -1}, "SECONDS_INVALID"),
        (
            "CONFIGURE_OWNER_REPLY_GRACE",
            {"seconds": 30, "extra": 1},
            "PARAMETERS_INVALID",
        ),
        ("SET_AUTOMATIC_RESPONSES", {"enabled": 1}, "ENABLED_INVALID"),
        ("APPROVE_RESPONSE_REVIEW", {"reference": "not a ref!"}, "REFERENCE_INVALID"),
    ],
)
def test_registry_parameter_validation_fails_closed(intent, parameters, reason):
    with pytest.raises(OwnerSemanticRegistryError, match=reason):
        normalize_semantic_parameters(intent, parameters)


def test_registry_materializes_only_available_existing_owner_actions():
    grace = materialize_owner_control_candidate(
        intent_key="CONFIGURE_OWNER_REPLY_GRACE",
        parameters={"seconds": 30},
    )
    assert grace == (
        OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS,
        GraceSecondsParameters(seconds=30),
    )

    automation = materialize_owner_control_candidate(
        intent_key="SET_AUTOMATIC_RESPONSES",
        parameters={"enabled": False},
    )
    assert automation == (
        OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
        AutomaticResponsesEnabledParameters(enabled=False),
    )

    review = materialize_owner_control_candidate(
        intent_key="APPROVE_RESPONSE_REVIEW",
        parameters={"reference": "ABCDEF12"},
    )
    assert review == (
        OwnerControlAction.APPROVE_RESPONSE_REVIEW,
        ReviewReferenceParameters(reference="abcdef12"),
    )

    unavailable = materialize_owner_control_candidate(
        intent_key="ONE_SHOT_REPLY_DELAY",
        parameters={"seconds": 30},
    )
    assert unavailable is None


def test_candidate_schema_rejects_model_invented_semantic_intent():
    with pytest.raises(ValidationError):
        OwnerControlSemanticCandidateOutput.model_validate(
            {
                "semantic_intent_key": "RUN_SHELL",
                "confidence": "high",
                "seconds": None,
                "enabled": None,
                "reference": None,
            }
        )


@pytest.mark.parametrize(
    "candidate",
    [
        raw_candidate(
            "CONFIGURE_OWNER_REPLY_GRACE",
            seconds=30,
            enabled=True,
        ),
        raw_candidate(
            "SET_AUTOMATIC_RESPONSES",
            enabled=True,
            seconds=30,
        ),
        raw_candidate(
            "APPROVE_RESPONSE_REVIEW",
            reference="abcdef12",
            enabled=True,
        ),
        raw_candidate(
            "ONE_SHOT_REPLY_DELAY",
            seconds=None,
        ),
    ],
)
def test_candidate_parameter_mismatch_is_rejected(candidate):
    with pytest.raises(
        OwnerControlCandidateBuilderError,
        match="CANDIDATE_PARAMETERS_INVALID",
    ):
        normalize_candidate_output(output(candidate))


def test_unresolved_requires_empty_candidate_list():
    unresolved = OwnerControlSemanticCandidateSetOutput(
        classification="UNRESOLVED",
        candidates=[],
    )
    assert normalize_candidate_output(unresolved) == []
    assert candidate_set_from_semantic_output(unresolved) is None

    with pytest.raises(
        OwnerControlCandidateBuilderError,
        match="UNRESOLVED_WITH_CANDIDATES",
    ):
        normalize_candidate_output(
            output(
                raw_candidate("ONE_SHOT_REPLY_DELAY", seconds=30),
                classification="UNRESOLVED",
            )
        )


def test_single_candidate_is_valid_for_yes_no_clarification():
    normalized = normalize_candidate_output(
        output(
            raw_candidate(
                "SET_AUTOMATIC_RESPONSES",
                enabled=False,
                confidence="medium",
            )
        )
    )
    assert len(normalized) == 1
    assert normalized[0]["semantic_intent_key"] == "SET_AUTOMATIC_RESPONSES"
    assert normalized[0]["confidence"] == "medium"


def test_retorne_30_candidate_set_preserves_materially_different_meanings():
    candidate_set = candidate_set_from_semantic_output(
        output(
            raw_candidate(
                "CONFIGURE_OWNER_REPLY_GRACE",
                seconds=30,
                confidence="high",
            ),
            raw_candidate(
                "ONE_SHOT_REPLY_DELAY",
                seconds=30,
                confidence="high",
            ),
        )
    )
    assert candidate_set is not None
    assert candidate_set["semantic_registry_version"] == OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION
    candidates = candidate_set["candidates"]
    assert {candidate["semantic_intent_key"] for candidate in candidates} == {
        "CONFIGURE_OWNER_REPLY_GRACE",
        "ONE_SHOT_REPLY_DELAY",
    }
    mappings = {
        candidate["semantic_intent_key"]: candidate["capability_mapping"]["status"]
        for candidate in candidates
    }
    assert mappings == {
        "CONFIGURE_OWNER_REPLY_GRACE": "AVAILABLE",
        "ONE_SHOT_REPLY_DELAY": "UNAVAILABLE",
    }


def test_model_candidate_order_does_not_change_candidate_set_fingerprint():
    grace = raw_candidate("CONFIGURE_OWNER_REPLY_GRACE", seconds=30)
    one_shot = raw_candidate("ONE_SHOT_REPLY_DELAY", seconds=30)
    first = candidate_set_from_semantic_output(output(grace, one_shot))
    second = candidate_set_from_semantic_output(output(one_shot, grace))
    assert first is not None and second is not None
    assert first == second
    assert candidate_set_fingerprint(first) == candidate_set_fingerprint(second)


def test_duplicate_semantic_candidate_is_rejected():
    same = raw_candidate("ONE_SHOT_REPLY_DELAY", seconds=30)
    with pytest.raises(
        OwnerControlCandidateBuilderError,
        match="CANDIDATE_DUPLICATE",
    ):
        normalize_candidate_output(output(same, same))


def test_interpret_candidate_builder_uses_structured_output_without_execution(monkeypatch):
    synthetic = output(
        raw_candidate("CONFIGURE_OWNER_REPLY_GRACE", seconds=30),
        raw_candidate("ONE_SHOT_REPLY_DELAY", seconds=30),
    )
    monkeypatch.setattr(
        owner_control_semantic_candidates,
        "_run_model",
        lambda text: synthetic if text == "retorne em 30 segundos" else pytest.fail(text),
    )
    candidate_set = interpret_owner_control_candidates("retorne em 30 segundos")
    assert candidate_set is not None
    assert len(candidate_set["candidates"]) == 2


def test_blank_text_never_calls_candidate_model(monkeypatch):
    monkeypatch.setattr(
        owner_control_semantic_candidates,
        "_run_model",
        lambda _text: pytest.fail("blank text must not invoke model"),
    )
    assert interpret_owner_control_candidates("   ") is None
