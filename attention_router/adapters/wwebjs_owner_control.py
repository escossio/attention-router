from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
import unicodedata

from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    GraceEnabledParameters,
    GraceSecondsParameters,
    OwnerControlAction,
    OwnerControlDispatchResult,
    OwnerControlParameters,
)


class OwnerControlParseStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_CONTROL_COMMAND = "NOT_CONTROL_COMMAND"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class OwnerControlParseResult:
    status: OwnerControlParseStatus
    action: OwnerControlAction | None = None
    parameters: OwnerControlParameters | None = None
    reason_code: str | None = None

    @property
    def consumed(self) -> bool:
        return self.status != OwnerControlParseStatus.NOT_CONTROL_COMMAND


_SECONDS_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"espera (?P<seconds>\d+)(?: segundos?)?",
        r"coloque a espera em (?P<seconds>\d+)(?: segundos?)?",
        r"mude a espera para (?P<seconds>\d+)(?: segundos?)?",
        r"mude o grace para (?P<seconds>\d+)(?: segundos?)?",
        r"tempo de espera (?P<seconds>\d+)(?: segundos?)?",
        r"defina a espera em (?P<seconds>\d+)(?: segundos?)?",
    )
)
_AUTO_RESPONSE_TARGET = r"(?:a resposta autom[aá]tica|as respostas autom[aá]ticas)"
_ENABLE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"retoma",
        r"retome",
        rf"ative {_AUTO_RESPONSE_TARGET}",
        rf"retome {_AUTO_RESPONSE_TARGET}",
    )
)
_DISABLE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"pausa",
        r"pause",
        rf"pause {_AUTO_RESPONSE_TARGET}",
        rf"desative {_AUTO_RESPONSE_TARGET}",
    )
)
_ADMINISTRATIVE_WRITTEN_SECONDS = (
    r"(?:zero|dez|vinte|trinta|quarenta|cinquenta|sessenta|setenta|"
    r"oitenta|noventa|cem|duzentos|trezentos)"
)
_ADMINISTRATIVE_PREFIX = re.compile(
    rf"(?:"
    rf"espera\s+(?:[+-]?\d|{_ADMINISTRATIVE_WRITTEN_SECONDS}\s+segundos?\b)|"
    r"(?:coloque a espera em|mude (?:a espera|o grace) para|"
    r"tempo de espera|defina a espera em)\s+\S|"
    rf"(?:ative|retome|pause|desative) {_AUTO_RESPONSE_TARGET}\b"
    r")"
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_ENABLE_WORD = re.compile(r"\b(?:ative|retoma|retome)\b")
_DISABLE_WORD = re.compile(r"\b(?:pausa|pause|desative)\b")
_CONTRADICTORY_COMMANDS = re.compile(
    rf"(?:pausa|pause|retoma|retome|ative|desative)"
    rf"(?: {_AUTO_RESPONSE_TARGET})? e "
    rf"(?:pausa|pause|retoma|retome|ative|desative)(?: {_AUTO_RESPONSE_TARGET})?"
)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    normalized = re.sub(r"^andy\s*[,;:]\s*", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.rstrip(".!?").strip()


def parse_owner_grace_control(text: str) -> OwnerControlParseResult:
    if "?" in unicodedata.normalize("NFKC", text):
        return OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)
    normalized = _normalize(text)
    if not normalized:
        return OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)
    for pattern in _SECONDS_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match:
            return OwnerControlParseResult(
                OwnerControlParseStatus.MATCHED,
                OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS,
                GraceSecondsParameters(seconds=int(match.group("seconds"))),
            )
    if any(pattern.fullmatch(normalized) for pattern in _ENABLE_PATTERNS):
        return OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=True),
        )
    if any(pattern.fullmatch(normalized) for pattern in _DISABLE_PATTERNS):
        return OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=False),
        )
    if (
        _CONTRADICTORY_COMMANDS.fullmatch(normalized)
        and _ENABLE_WORD.search(normalized)
        and _DISABLE_WORD.search(normalized)
    ):
        return OwnerControlParseResult(
            OwnerControlParseStatus.REJECTED, reason_code="CONTROL_COMMAND_AMBIGUOUS"
        )
    if not _ADMINISTRATIVE_PREFIX.match(normalized):
        return OwnerControlParseResult(OwnerControlParseStatus.NOT_CONTROL_COMMAND)
    numbers = _NUMBER.findall(normalized)
    enable = bool(_ENABLE_WORD.search(normalized))
    disable = bool(_DISABLE_WORD.search(normalized))
    if len(numbers) > 1 or (enable and disable) or (numbers and (enable or disable)):
        return OwnerControlParseResult(
            OwnerControlParseStatus.REJECTED,
            reason_code="CONTROL_COMMAND_AMBIGUOUS",
        )
    return OwnerControlParseResult(
        OwnerControlParseStatus.REJECTED,
        reason_code="CONTROL_COMMAND_INVALID_VALUE",
    )


def canonical_command_text(result: OwnerControlParseResult) -> str:
    if result.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED:
        assert isinstance(result.parameters, AutomaticResponsesEnabledParameters)
        value = "true" if result.parameters.enabled else "false"
        return f"SET_AUTOMATIC_RESPONSES_ENABLED enabled={value}"
    if result.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS:
        assert isinstance(result.parameters, GraceSecondsParameters)
        return f"SET_OWNER_REPLY_GRACE_SECONDS seconds={result.parameters.seconds}"
    if result.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_ENABLED:
        assert isinstance(result.parameters, GraceEnabledParameters)
        value = "true" if result.parameters.enabled else "false"
        return f"SET_OWNER_REPLY_GRACE_ENABLED enabled={value}"
    return f"OWNER_CONTROL_REJECTED reason={result.reason_code or 'UNKNOWN'}"


def render_owner_control_confirmation(
    result: OwnerControlDispatchResult,
    *,
    action: OwnerControlAction,
) -> str:
    control = result.mutation.control
    if action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED:
        if control.automatic_responses_enabled:
            return (
                "Andy retomada. Respostas automáticas novamente ativas."
                if result.mutation.changed else "Andy já está ativa."
            )
        return (
            "Andy pausada. Continuo recebendo mensagens, mas não vou responder automaticamente."
            if result.mutation.changed else "Andy já está pausada."
        )
    if action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS:
        if not control.enabled:
            return (
                f"Tempo ajustado para {control.effective_seconds} segundos. "
                "A espera automática desta policy continua desativada."
            )
        return f"Espera alterada para {control.effective_seconds} segundos."
    if control.enabled:
        return f"Espera automática desta policy ativada: {control.effective_seconds} segundos."
    return "Espera automática desta policy desativada."


def render_owner_control_error(reason_code: str) -> str:
    if reason_code == "CONTROL_COMMAND_AMBIGUOUS":
        return "Não consegui aplicar o comando: comando ambíguo."
    if reason_code in {"CONTROL_COMMAND_INVALID_VALUE", "GRACE_SECONDS_MUST_BE_INTEGER"}:
        return "Não consegui alterar a espera: valor inválido."
    if reason_code == "GRACE_SECONDS_OUT_OF_POLICY_BOUNDS":
        return "Não consegui alterar a espera: valor fora do limite permitido."
    if reason_code == "OWNER_CONTROL_GRACE_POLICY_UNAVAILABLE":
        return "Não consegui alterar a espera: configuração indisponível."
    if reason_code == "OWNER_CONTROL_GRACE_POLICY_AMBIGUOUS":
        return "Não consegui alterar a espera: há mais de uma configuração aplicável."
    return "Não consegui alterar a espera."


__all__ = [
    "OwnerControlParseResult",
    "OwnerControlParseStatus",
    "canonical_command_text",
    "parse_owner_grace_control",
    "render_owner_control_confirmation",
    "render_owner_control_error",
]
