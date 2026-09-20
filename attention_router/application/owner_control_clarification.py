from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata
from typing import Any

from attention_router.application.owner_control_semantic_registry import (
    OwnerSemanticRegistryError,
    normalize_semantic_parameters,
    semantic_intent_definitions,
)


class OwnerControlClarificationError(ValueError):
    pass


class OwnerClarificationResolutionKind(StrEnum):
    SELECT = "SELECT"
    CANCEL = "CANCEL"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class OwnerClarificationResolution:
    kind: OwnerClarificationResolutionKind
    candidate_key: str | None = None


_ORDINALS = {
    1: {"primeira", "a primeira", "primeiro", "o primeiro"},
    2: {"segunda", "a segunda", "segundo", "o segundo"},
    3: {"terceira", "a terceira", "terceiro", "o terceiro"},
    4: {"quarta", "a quarta", "quarto", "o quarto"},
    5: {"quinta", "a quinta", "quinto", "o quinto"},
    6: {"sexta", "a sexta", "sexto", "o sexto"},
    7: {"setima", "a setima", "setimo", "o setimo"},
    8: {"oitava", "a oitava", "oitavo", "o oitavo"},
}
_YES = {"sim", "isso", "isso mesmo", "correto", "exato"}
_NO = {"nao", "negativo"}
_CANCEL = {
    "cancelar",
    "cancela",
    "cancele",
    "nenhuma",
    "nenhuma delas",
    "nenhuma das opcoes",
    "nenhum",
    "nenhuma opcao",
}


def _normalized_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_text = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    )
    ascii_text = re.sub(r"[^a-z0-9\s]", " ", ascii_text)
    return " ".join(ascii_text.split())


def _ordered_candidates(candidate_set: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(candidate_set, dict):
        raise OwnerControlClarificationError(
            "OWNER_CLARIFICATION_CANDIDATE_SET_INVALID"
        )
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 8:
        raise OwnerControlClarificationError(
            "OWNER_CLARIFICATION_CANDIDATE_SET_INVALID"
        )

    registry_order = {
        definition.key.value: index
        for index, definition in enumerate(semantic_intent_definitions())
    }
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise OwnerControlClarificationError(
                "OWNER_CLARIFICATION_CANDIDATE_INVALID"
            )
        key = candidate.get("candidate_key")
        semantic_key = candidate.get("semantic_intent_key")
        parameters = candidate.get("parameters")
        mapping = candidate.get("capability_mapping")
        if (
            not isinstance(key, str)
            or not key
            or key in seen
            or semantic_key not in registry_order
            or not isinstance(parameters, dict)
            or not isinstance(mapping, dict)
        ):
            raise OwnerControlClarificationError(
                "OWNER_CLARIFICATION_CANDIDATE_INVALID"
            )
        try:
            normalized_parameters = normalize_semantic_parameters(
                semantic_key, parameters
            )
        except OwnerSemanticRegistryError as exc:
            raise OwnerControlClarificationError(str(exc)) from exc
        normalized.append(
            {
                **candidate,
                "candidate_key": key,
                "semantic_intent_key": semantic_key,
                "parameters": normalized_parameters,
            }
        )
        seen.add(key)

    normalized.sort(
        key=lambda candidate: (
            registry_order[candidate["semantic_intent_key"]],
            candidate["candidate_key"],
        )
    )
    return normalized


def _candidate_label(
    candidate: dict[str, Any], *, include_availability: bool = True
) -> str:
    key = candidate["semantic_intent_key"]
    params = candidate["parameters"]
    if key == "CONFIGURE_OWNER_REPLY_GRACE":
        label = (
            "mudar a espera padrão das respostas para "
            f"{params['seconds']} segundos"
        )
    elif key == "SET_AUTOMATIC_RESPONSES":
        label = (
            "ativar as respostas automáticas"
            if params["enabled"]
            else "desativar as respostas automáticas"
        )
    elif key == "APPROVE_RESPONSE_REVIEW":
        label = f"aprovar a revisão {params['reference']}"
    elif key == "REJECT_RESPONSE_REVIEW":
        label = f"rejeitar a revisão {params['reference']}"
    elif key == "ONE_SHOT_REPLY_DELAY":
        label = (
            f"esperar {params['seconds']} segundos apenas nesta resposta"
        )
    else:
        raise OwnerControlClarificationError(
            "OWNER_CLARIFICATION_CANDIDATE_INVALID"
        )

    mapping = candidate.get("capability_mapping") or {}
    if include_availability and mapping.get("status") == "UNAVAILABLE":
        return f"{label} (ainda indisponível)"
    return label


def render_owner_control_clarification(
    candidate_set: dict[str, Any],
) -> str:
    candidates = _ordered_candidates(candidate_set)
    if len(candidates) == 1:
        return (
            f"Você quis dizer: {_candidate_label(candidates[0])}? "
            'Responda "sim" ou "não".'
        )
    options = "; ".join(
        f"{index}) {_candidate_label(candidate)}"
        for index, candidate in enumerate(candidates, start=1)
    )
    return (
        f"Fiquei em dúvida. Você quis: {options}? "
        'Responda com o número da opção, por exemplo "1" ou "2".'
    )


def render_owner_control_clarification_unavailable(
    candidate: dict[str, Any],
) -> str:
    validated = _ordered_candidates({"candidates": [candidate]})[0]
    return (
        f"Entendi: {_candidate_label(validated, include_availability=False)}. "
        "Essa opção ainda não está disponível, então nada foi executado."
    )


def render_owner_control_clarification_canceled() -> str:
    return "Certo. Não vou executar nenhuma das opções."


def selected_candidate(
    candidate_set: dict[str, Any], candidate_key: str
) -> dict[str, Any]:
    for candidate in _ordered_candidates(candidate_set):
        if candidate["candidate_key"] == candidate_key:
            return candidate
    raise OwnerControlClarificationError(
        "OWNER_CLARIFICATION_CANDIDATE_NOT_FOUND"
    )


def _indexed_selection(text: str, candidate_count: int) -> int | None:
    for index in range(1, candidate_count + 1):
        forms = {
            str(index),
            f"opcao {index}",
            f"opcao numero {index}",
            f"numero {index}",
            *_ORDINALS[index],
        }
        if text in forms:
            return index
    return None


def resolve_owner_control_clarification_reply(
    text: str,
    candidate_set: dict[str, Any],
) -> OwnerClarificationResolution:
    candidates = _ordered_candidates(candidate_set)
    normalized = _normalized_text(text)
    if not normalized:
        return OwnerClarificationResolution(
            OwnerClarificationResolutionKind.UNRESOLVED
        )

    selected_index = _indexed_selection(normalized, len(candidates))
    if selected_index is not None:
        return OwnerClarificationResolution(
            OwnerClarificationResolutionKind.SELECT,
            candidates[selected_index - 1]["candidate_key"],
        )

    if normalized in _CANCEL:
        return OwnerClarificationResolution(
            OwnerClarificationResolutionKind.CANCEL
        )

    if len(candidates) == 1:
        if normalized in _YES:
            return OwnerClarificationResolution(
                OwnerClarificationResolutionKind.SELECT,
                candidates[0]["candidate_key"],
            )
        if normalized in _NO:
            return OwnerClarificationResolution(
                OwnerClarificationResolutionKind.CANCEL
            )

    return OwnerClarificationResolution(
        OwnerClarificationResolutionKind.UNRESOLVED
    )


__all__ = [
    "OwnerClarificationResolution",
    "OwnerClarificationResolutionKind",
    "OwnerControlClarificationError",
    "render_owner_control_clarification",
    "render_owner_control_clarification_canceled",
    "render_owner_control_clarification_unavailable",
    "resolve_owner_control_clarification_reply",
    "selected_candidate",
]
