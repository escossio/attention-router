"""Deterministic interpretation boundary for owner-controlled time expressions.

The language model may propose a quantity, unit, intent, scope and condition.
It never supplies the clock, computed deadline, authority or permission to send.
This module has no database, network, scheduler or command side effects.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class TemporalValidationError(ValueError):
    """Invalid, ambiguous or out-of-bound temporal expression."""


class TemporalUnit(StrEnum):
    SECOND = "SECOND"
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    YEAR = "YEAR"


class TemporalIntention(StrEnum):
    ONE_SHOT_REPLY = "ONE_SHOT_REPLY"
    DEFAULT_REPLY_GRACE = "DEFAULT_REPLY_GRACE"
    AUTOMATION_PAUSE = "AUTOMATION_PAUSE"
    UNRESOLVED = "UNRESOLVED"


class TemporalScope(StrEnum):
    CURRENT_CONVERSATION = "CURRENT_CONVERSATION"
    GLOBAL = "GLOBAL"
    UNRESOLVED = "UNRESOLVED"


class TemporalCondition(StrEnum):
    NO_OWNER_REPLY = "NO_OWNER_REPLY"
    UNCONDITIONAL = "UNCONDITIONAL"
    UNRESOLVED = "UNRESOLVED"


class TemporalProposal(BaseModel):
    """Strict schema accepted from an LLM or deterministic language adapter."""

    model_config = ConfigDict(extra="forbid", strict=True)

    quantity: int
    unit: TemporalUnit
    intention: TemporalIntention
    scope: TemporalScope
    condition: TemporalCondition

    @field_validator("unit", "intention", "scope", "condition", mode="before")
    @classmethod
    def accept_exact_enum_literal(cls, value: object, info):
        types = {
            "unit": TemporalUnit,
            "intention": TemporalIntention,
            "scope": TemporalScope,
            "condition": TemporalCondition,
        }
        enum_type = types[info.field_name]
        if type(value) is str:
            try:
                return enum_type(value)
            except ValueError:
                pass
        return value


@dataclass(frozen=True, slots=True)
class ResolvedTemporalProposal:
    quantity: int
    unit: TemporalUnit
    intention: TemporalIntention
    scope: TemporalScope
    condition: TemporalCondition
    reference_utc: datetime
    due_at_utc: datetime
    timezone_name: str
    calendar_policy: str

    @property
    def is_authorized_to_schedule(self) -> bool:
        # Interpretation is never authorization, regardless of completeness.
        return False

    def to_evidence(self) -> dict[str, object]:
        """Sanitized, serializable evidence. Not a command or scheduler ticket."""
        return {
            "quantity": self.quantity,
            "unit": self.unit.value,
            "intention": self.intention.value,
            "scope": self.scope.value,
            "condition": self.condition.value,
            "reference_utc": self.reference_utc.isoformat(),
            "due_at_utc": self.due_at_utc.isoformat(),
            "timezone_name": self.timezone_name,
            "calendar_policy": self.calendar_policy,
        }


_MAX_SECONDS = 10 * 366 * 86400
_ELAPSED_SECONDS = {
    TemporalUnit.SECOND: 1,
    TemporalUnit.MINUTE: 60,
    TemporalUnit.HOUR: 3600,
    TemporalUnit.DAY: 86400,
    TemporalUnit.WEEK: 604800,
}

_NORMALIZED_UNITS = {
    "segundo": TemporalUnit.SECOND, "segundos": TemporalUnit.SECOND,
    "minuto": TemporalUnit.MINUTE, "minutos": TemporalUnit.MINUTE,
    "hora": TemporalUnit.HOUR, "horas": TemporalUnit.HOUR,
    "dia": TemporalUnit.DAY, "dias": TemporalUnit.DAY,
    "semana": TemporalUnit.WEEK, "semanas": TemporalUnit.WEEK,
    "mes": TemporalUnit.MONTH, "meses": TemporalUnit.MONTH,
    "ano": TemporalUnit.YEAR, "anos": TemporalUnit.YEAR,
}

_NUMBERS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3,
    "quatro": 4, "cinco": 5, "seis": 6,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "quinze": 15, "vinte": 20, "trinta": 30,
    "sessenta": 60, "noventa": 90,
}


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    value = " ".join(re.sub(r"[^a-z0-9\s]", " ", ascii_text).split())
    return re.sub(r"^andy\s+", "", value)


def propose_from_portuguese(text: str) -> TemporalProposal | None:
    """Extract a simple explicit duration without granting any action.

    Never guess a destination or global setting from bare "aguarde um ano".
    Complex language may instead be proposed by the existing semantic model.
    """
    if not isinstance(text, str) or not text.strip() or any(
        marker in text for marker in ('?', '"', "'", '“', '”', '«', '»')
    ):
        return None
    value = _normalized(text)
    match = re.fullmatch(
        r"(?:aguarde|espere|espera|retorne em|responda em|volte em|"
        r"pause as respostas automaticas por|"
        r"passe a esperar|configure a espera padrao para)"
        r"\s+(?P<quantity>\d+|[a-z]+)\s+(?P<unit>segundos?|minutos?|horas?|"
        r"dias?|semanas?|mes(?:es)?|anos?)"
        r"(?P<qualifier>.*)",
        value,
    )
    if match is None:
        return None
    raw = match["quantity"]
    amount = int(raw) if raw.isdigit() else _NUMBERS.get(raw)
    if amount is None:
        return None
    unit = _NORMALIZED_UNITS.get(match["unit"])
    if unit is None:
        return None

    prefix = value[:match.start("quantity")].strip()
    qualifier = match["qualifier"].strip()
    intention = TemporalIntention.UNRESOLVED
    scope = TemporalScope.UNRESOLVED
    condition = TemporalCondition.UNRESOLVED
    if prefix.startswith("pause as respostas automaticas por") and not qualifier:
        intention = TemporalIntention.AUTOMATION_PAUSE
        scope = TemporalScope.GLOBAL
        condition = TemporalCondition.UNCONDITIONAL
    elif prefix.startswith(("passe a esperar", "configure a espera padrao para")) and qualifier in {
        "", "antes de responder", "antes de responder automaticamente"
    }:
        intention = TemporalIntention.DEFAULT_REPLY_GRACE
        scope = TemporalScope.GLOBAL
        condition = TemporalCondition.NO_OWNER_REPLY
    elif qualifier in {
        "nesta conversa se eu nao responder",
        "se eu nao responder nesta conversa",
    }:
        intention = TemporalIntention.ONE_SHOT_REPLY
        scope = TemporalScope.CURRENT_CONVERSATION
        condition = TemporalCondition.NO_OWNER_REPLY
    elif qualifier in {"para responder", "antes de responder"}:
        # An explicit reply target does not distinguish one-shot vs persistent:
        # retain only the duration, leaving intent/scope unresolved for
        # Clarification rather than choosing a dangerous global mutation.
        pass
    elif qualifier:
        # Additional unsupported clauses must never be silently discarded:
        # a model can analyze them, but deterministic fallback cannot.
        return None
    return TemporalProposal(
        quantity=amount,
        unit=unit,
        intention=intention,
        scope=scope,
        condition=condition,
    )


TEMPORAL_PERIOD_PROVENANCE_FIELD = "temporal_period_v1"


def normalized_period(
    quantity: int,
    unit: TemporalUnit | str,
    *,
    timezone_name: str,
) -> dict[str, object]:
    """Trusted, bounded period encoding for persistent owner-grace controls.

    The integer fallback is a legacy compatibility value. Calendar deadlines
    are *always* calculated per inbound event, never using this approximation.
    """
    period = TemporalProposal.model_validate({
        "quantity": quantity,
        "unit": unit,
        "intention": "DEFAULT_REPLY_GRACE",
        "scope": "GLOBAL",
        "condition": "NO_OWNER_REPLY",
    })
    validated = validate_temporal_proposal(period)
    if not isinstance(timezone_name, str):
        raise TemporalValidationError("TEMPORAL_TIMEZONE_INVALID")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TemporalValidationError("TEMPORAL_TIMEZONE_INVALID") from exc
    unit_seconds = {
        **_ELAPSED_SECONDS,
        TemporalUnit.MONTH: 30 * 86400,
        TemporalUnit.YEAR: 365 * 86400,
    }
    if validated.unit == TemporalUnit.YEAR and validated.quantity > 10:
        raise TemporalValidationError("TEMPORAL_HORIZON_EXCEEDED")
    if validated.unit == TemporalUnit.MONTH and validated.quantity > 120:
        raise TemporalValidationError("TEMPORAL_HORIZON_EXCEEDED")
    approx = validated.quantity * unit_seconds[validated.unit]
    if approx > _MAX_SECONDS:
        raise TemporalValidationError("TEMPORAL_HORIZON_EXCEEDED")
    return {
        "quantity": validated.quantity,
        "unit": validated.unit.value,
        "timezone_name": timezone_name,
    }


def period_legacy_seconds(period: dict[str, object]) -> int:
    """Return a bounded compatibility integer; not a calendar timer."""
    if not isinstance(period, dict) or set(period) != {
        "quantity", "unit", "timezone_name",
    }:
        raise TemporalValidationError("TEMPORAL_PERIOD_INVALID")
    normalized = normalized_period(
        period["quantity"], period["unit"], timezone_name=period["timezone_name"]
    )
    unit = TemporalUnit(normalized["unit"])
    unit_seconds = {
        **_ELAPSED_SECONDS,
        TemporalUnit.MONTH: 30 * 86400,
        TemporalUnit.YEAR: 365 * 86400,
    }
    return normalized["quantity"] * unit_seconds[unit]


def deadline_for_period(reference: datetime, period: dict[str, object]) -> datetime:
    """Compute an absolute UTC deadline from a stored immutable period."""
    period_legacy_seconds(period)  # full bounds and shape validation
    p = TemporalProposal(
        quantity=period["quantity"],
        unit=period["unit"],
        intention=TemporalIntention.DEFAULT_REPLY_GRACE,
        scope=TemporalScope.GLOBAL,
        condition=TemporalCondition.NO_OWNER_REPLY,
    )
    return resolve_temporal_proposal(
        p, reference=reference, timezone_name=period["timezone_name"]
    ).due_at_utc


def _calendar_date(local: datetime, *, months: int) -> datetime:
    month_index = local.year * 12 + local.month - 1 + months
    year, offset = divmod(month_index, 12)
    if not 1 <= year <= 9999:
        raise TemporalValidationError("TEMPORAL_YEAR_OUT_OF_RANGE")
    month = offset + 1
    day = min(local.day, monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


def _strict_local_timezone(local: datetime, zone: ZoneInfo) -> datetime:
    """Reject local clock gaps and folds rather than silently choosing an offset."""
    naive = local.replace(tzinfo=None)
    matches = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        actual = candidate.astimezone(timezone.utc).astimezone(zone)
        if actual.replace(tzinfo=None) == naive:
            matches.append(candidate)
    if not matches:
        raise TemporalValidationError("TEMPORAL_NONEXISTENT_LOCAL_TIME")
    if len(matches) == 2 and matches[0].utcoffset() != matches[1].utcoffset():
        raise TemporalValidationError("TEMPORAL_AMBIGUOUS_LOCAL_TIME")
    return matches[0]


def validate_temporal_proposal(value: TemporalProposal | dict[str, object]) -> TemporalProposal:
    try:
        proposal = TemporalProposal.model_validate(value)
    except (ValueError, ValidationError, TypeError) as exc:
        raise TemporalValidationError("TEMPORAL_PROPOSAL_INVALID") from exc
    if not 0 < proposal.quantity <= _MAX_SECONDS:
        raise TemporalValidationError("TEMPORAL_QUANTITY_OUT_OF_RANGE")
    if proposal.intention == TemporalIntention.ONE_SHOT_REPLY and (
        proposal.scope != TemporalScope.CURRENT_CONVERSATION
        or proposal.condition != TemporalCondition.NO_OWNER_REPLY
    ):
        raise TemporalValidationError("TEMPORAL_SCOPE_CONDITION_MISMATCH")
    if proposal.intention == TemporalIntention.DEFAULT_REPLY_GRACE and (
        proposal.scope != TemporalScope.GLOBAL
        or proposal.condition != TemporalCondition.NO_OWNER_REPLY
    ):
        raise TemporalValidationError("TEMPORAL_SCOPE_CONDITION_MISMATCH")
    if proposal.intention == TemporalIntention.AUTOMATION_PAUSE and (
        proposal.scope != TemporalScope.GLOBAL
        or proposal.condition != TemporalCondition.UNCONDITIONAL
    ):
        raise TemporalValidationError("TEMPORAL_SCOPE_CONDITION_MISMATCH")
    if proposal.intention == TemporalIntention.UNRESOLVED and (
        proposal.scope != TemporalScope.UNRESOLVED
        or proposal.condition != TemporalCondition.UNRESOLVED
    ):
        raise TemporalValidationError("TEMPORAL_SCOPE_CONDITION_MISMATCH")
    return proposal


def resolve_temporal_proposal(
    proposal: TemporalProposal | dict[str, object],
    *,
    reference: datetime,
    timezone_name: str,
) -> ResolvedTemporalProposal:
    """Resolve from a *trusted* clock and IANA timezone, not the LLM's arithmetic."""
    parsed = validate_temporal_proposal(proposal)
    if not isinstance(reference, datetime) or reference.tzinfo is None:
        raise TemporalValidationError("TEMPORAL_REFERENCE_MUST_BE_AWARE")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise TemporalValidationError("TEMPORAL_TIMEZONE_INVALID")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TemporalValidationError("TEMPORAL_TIMEZONE_INVALID") from exc
    reference_utc = reference.astimezone(timezone.utc)
    if parsed.unit in _ELAPSED_SECONDS:
        seconds = parsed.quantity * _ELAPSED_SECONDS[parsed.unit]
        if seconds > _MAX_SECONDS:
            raise TemporalValidationError("TEMPORAL_HORIZON_EXCEEDED")
        deadline_utc = reference_utc + timedelta(seconds=seconds)
        policy = "ELAPSED_UTC_SECONDS"
    else:
        months = parsed.quantity * (12 if parsed.unit == TemporalUnit.YEAR else 1)
        if months > 120:
            raise TemporalValidationError("TEMPORAL_HORIZON_EXCEEDED")
        local = reference_utc.astimezone(zone)
        naive_destination = _calendar_date(local.replace(tzinfo=None), months=months)
        deadline_utc = _strict_local_timezone(naive_destination, zone).astimezone(timezone.utc)
        policy = "CALENDAR_CLAMP_LAST_DAY_REJECT_DST_GAP_OR_FOLD"
    if deadline_utc <= reference_utc:
        raise TemporalValidationError("TEMPORAL_DEADLINE_NOT_FUTURE")
    return ResolvedTemporalProposal(
        quantity=parsed.quantity,
        unit=parsed.unit,
        intention=parsed.intention,
        scope=parsed.scope,
        condition=parsed.condition,
        reference_utc=reference_utc,
        due_at_utc=deadline_utc,
        timezone_name=timezone_name,
        calendar_policy=policy,
    )


__all__ = [
    "ResolvedTemporalProposal",
    "TemporalCondition",
    "TemporalIntention",
    "TemporalProposal",
    "TemporalScope",
    "TemporalUnit",
    "TemporalValidationError",
    "TEMPORAL_PERIOD_PROVENANCE_FIELD",
    "normalized_period",
    "period_legacy_seconds",
    "deadline_for_period",
    "propose_from_portuguese",
    "resolve_temporal_proposal",
    "validate_temporal_proposal",
]
