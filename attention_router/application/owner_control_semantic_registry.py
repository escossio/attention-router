from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    GraceSecondsParameters,
    OwnerControlAction,
    OwnerControlParameters,
    ReviewReferenceParameters,
)
from attention_router.application.pending_intent import CANDIDATE_CONFIDENCE
from attention_router.infrastructure.hashing import stable_hash


OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION = "owner-control-semantic-v2"


class OwnerSemanticIntentKey(StrEnum):
    CONFIGURE_OWNER_REPLY_GRACE = "CONFIGURE_OWNER_REPLY_GRACE"
    SET_AUTOMATIC_RESPONSES = "SET_AUTOMATIC_RESPONSES"
    APPROVE_RESPONSE_REVIEW = "APPROVE_RESPONSE_REVIEW"
    REJECT_RESPONSE_REVIEW = "REJECT_RESPONSE_REVIEW"
    ONE_SHOT_REPLY_DELAY = "ONE_SHOT_REPLY_DELAY"


class OwnerSemanticRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerSemanticIntentDefinition:
    key: OwnerSemanticIntentKey
    capability_status: str
    capability_key: str | None
    owner_control_action: OwnerControlAction | None
    parameter_kind: str


_DEFINITIONS: dict[OwnerSemanticIntentKey, OwnerSemanticIntentDefinition] = {
    OwnerSemanticIntentKey.CONFIGURE_OWNER_REPLY_GRACE: OwnerSemanticIntentDefinition(
        key=OwnerSemanticIntentKey.CONFIGURE_OWNER_REPLY_GRACE,
        capability_status="AVAILABLE",
        capability_key="owner_control:SET_OWNER_REPLY_GRACE_SECONDS",
        owner_control_action=OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS,
        parameter_kind="SECONDS",
    ),
    OwnerSemanticIntentKey.SET_AUTOMATIC_RESPONSES: OwnerSemanticIntentDefinition(
        key=OwnerSemanticIntentKey.SET_AUTOMATIC_RESPONSES,
        capability_status="AVAILABLE",
        capability_key="owner_control:SET_AUTOMATIC_RESPONSES_ENABLED",
        owner_control_action=OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
        parameter_kind="ENABLED",
    ),
    OwnerSemanticIntentKey.APPROVE_RESPONSE_REVIEW: OwnerSemanticIntentDefinition(
        key=OwnerSemanticIntentKey.APPROVE_RESPONSE_REVIEW,
        capability_status="AVAILABLE",
        capability_key="owner_control:APPROVE_RESPONSE_REVIEW",
        owner_control_action=OwnerControlAction.APPROVE_RESPONSE_REVIEW,
        parameter_kind="REFERENCE",
    ),
    OwnerSemanticIntentKey.REJECT_RESPONSE_REVIEW: OwnerSemanticIntentDefinition(
        key=OwnerSemanticIntentKey.REJECT_RESPONSE_REVIEW,
        capability_status="AVAILABLE",
        capability_key="owner_control:REJECT_RESPONSE_REVIEW",
        owner_control_action=OwnerControlAction.REJECT_RESPONSE_REVIEW,
        parameter_kind="REFERENCE",
    ),
    OwnerSemanticIntentKey.ONE_SHOT_REPLY_DELAY: OwnerSemanticIntentDefinition(
        key=OwnerSemanticIntentKey.ONE_SHOT_REPLY_DELAY,
        capability_status="UNAVAILABLE",
        capability_key=None,
        owner_control_action=None,
        parameter_kind="SECONDS",
    ),
}


def semantic_intent_definitions() -> tuple[OwnerSemanticIntentDefinition, ...]:
    return tuple(_DEFINITIONS[key] for key in OwnerSemanticIntentKey)


def semantic_intent_definition(
    intent_key: OwnerSemanticIntentKey | str,
) -> OwnerSemanticIntentDefinition:
    try:
        key = (
            intent_key
            if isinstance(intent_key, OwnerSemanticIntentKey)
            else OwnerSemanticIntentKey(intent_key)
        )
    except (TypeError, ValueError) as exc:
        raise OwnerSemanticRegistryError(
            "OWNER_SEMANTIC_INTENT_UNREGISTERED"
        ) from exc
    return _DEFINITIONS[key]


def _require_exact_keys(parameters: dict[str, Any], expected: set[str]) -> None:
    if set(parameters) != expected:
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_PARAMETERS_INVALID")


def _normalize_seconds(parameters: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(parameters, {"seconds"})
    seconds = parameters.get("seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_SECONDS_INVALID")
    return {"seconds": seconds}


def _normalize_enabled(parameters: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(parameters, {"enabled"})
    enabled = parameters.get("enabled")
    if not isinstance(enabled, bool):
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_ENABLED_INVALID")
    return {"enabled": enabled}


def _normalize_reference(parameters: dict[str, Any]) -> dict[str, Any]:
    _require_exact_keys(parameters, {"reference"})
    reference = parameters.get("reference")
    if not isinstance(reference, str):
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_REFERENCE_INVALID")
    try:
        normalized = ReviewReferenceParameters(reference=reference).reference
    except Exception as exc:
        raise OwnerSemanticRegistryError(
            "OWNER_SEMANTIC_REFERENCE_INVALID"
        ) from exc
    return {"reference": normalized}


def normalize_semantic_parameters(
    intent_key: OwnerSemanticIntentKey | str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_PARAMETERS_INVALID")
    definition = semantic_intent_definition(intent_key)
    if definition.parameter_kind == "SECONDS":
        return _normalize_seconds(parameters)
    if definition.parameter_kind == "ENABLED":
        return _normalize_enabled(parameters)
    if definition.parameter_kind == "REFERENCE":
        return _normalize_reference(parameters)
    raise OwnerSemanticRegistryError("OWNER_SEMANTIC_PARAMETER_KIND_UNSUPPORTED")


def _candidate_key(
    intent_key: OwnerSemanticIntentKey,
    parameters: dict[str, Any],
) -> str:
    digest = stable_hash(
        {
            "semantic_registry_version": OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
            "semantic_intent_key": intent_key.value,
            "parameters": parameters,
        }
    )
    return f"semantic-{digest[:32]}"


def build_registered_semantic_candidate(
    *,
    intent_key: OwnerSemanticIntentKey | str,
    parameters: dict[str, Any],
    confidence: str,
) -> dict[str, Any]:
    if confidence not in CANDIDATE_CONFIDENCE:
        raise OwnerSemanticRegistryError("OWNER_SEMANTIC_CONFIDENCE_INVALID")
    definition = semantic_intent_definition(intent_key)
    normalized = normalize_semantic_parameters(definition.key, parameters)
    return {
        "candidate_key": _candidate_key(definition.key, normalized),
        "semantic_intent_key": definition.key.value,
        "parameters": normalized,
        "confidence": confidence,
        "evidence_summary": None,
        "capability_mapping": {
            "status": definition.capability_status,
            "capability_key": definition.capability_key,
        },
    }


def materialize_owner_control_candidate(
    *,
    intent_key: OwnerSemanticIntentKey | str,
    parameters: dict[str, Any],
) -> tuple[OwnerControlAction, OwnerControlParameters] | None:
    definition = semantic_intent_definition(intent_key)
    normalized = normalize_semantic_parameters(definition.key, parameters)
    action = definition.owner_control_action
    if action is None:
        return None
    if action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS:
        typed: OwnerControlParameters = GraceSecondsParameters(
            seconds=normalized["seconds"]
        )
    elif action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED:
        typed = AutomaticResponsesEnabledParameters(enabled=normalized["enabled"])
    elif action in {
        OwnerControlAction.APPROVE_RESPONSE_REVIEW,
        OwnerControlAction.REJECT_RESPONSE_REVIEW,
    }:
        typed = ReviewReferenceParameters(reference=normalized["reference"])
    else:
        raise OwnerSemanticRegistryError(
            "OWNER_SEMANTIC_EXECUTION_MAPPING_UNSUPPORTED"
        )
    return action, typed


__all__ = [
    "OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION",
    "OwnerSemanticIntentDefinition",
    "OwnerSemanticIntentKey",
    "OwnerSemanticRegistryError",
    "build_registered_semantic_candidate",
    "materialize_owner_control_candidate",
    "normalize_semantic_parameters",
    "semantic_intent_definition",
    "semantic_intent_definitions",
]
