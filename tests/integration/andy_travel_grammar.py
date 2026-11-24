"""Frozen deterministic grammar from the previous travel demonstration."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import re


STATEMENT = re.compile(
    r"(Planejamos|Proponho mudar|Confirmo) a viagem para (sábado|domingo)"
    r"(?:, (\d{2}/\d{2}/\d{4}))?\.?$", re.IGNORECASE,
)
ACTIONS = {"planejamos": "PROPOSES", "proponho mudar": "COUNTERPROPOSES", "confirmo": "CONFIRMS"}
WEEKDAYS = {"sábado": 5, "domingo": 6}


@dataclass(frozen=True)
class Interpretation:
    action: str
    date: str
    confidence: float
    parser: str = "PTBR_STRUCTURED_TRAVEL_V1"


def interpret(text: str, sent_at: datetime) -> Interpretation | None:
    match = STATEMENT.fullmatch(text.strip())
    if match is None:
        return None
    action, weekday, explicit_date = match.groups()
    weekday_number = WEEKDAYS[weekday.lower()]
    if explicit_date:
        try:
            event_date = datetime.strptime(explicit_date, "%d/%m/%Y").date()
        except ValueError:
            return None
        if event_date.weekday() != weekday_number:
            return None
    else:
        delay = (weekday_number - sent_at.weekday()) % 7
        event_date = sent_at.date() + timedelta(days=delay)
    return Interpretation(ACTIONS[action.lower()], event_date.isoformat(), 0.7)
