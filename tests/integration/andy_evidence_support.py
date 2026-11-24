"""Conservative support for a bounded set of statements in the semantic lab."""

from dataclasses import dataclass, replace
from datetime import date
import re
from typing import Literal

from pydantic import Field

from andy_semantic_contract import Contract, Mode, Polarity


_TOKEN = re.compile(r"[^\W\d_]+|[0-9]+|\S")
_ARTICLES = {"a", "o", "as", "os"}
_OBJECTS = {"reunião", "laudo", "documento", "orçamento"}
_DEICTIC = {"aquilo", "isso", "isto", "quem", "ele", "ela", "eles", "elas", "alguém", "aquele", "aquela"}
_VERBS = {
    "confirmou": "confirm_meeting", "marcou": "schedule_meeting", "agendou": "schedule_meeting",
    "realizou": "hold_meeting", "enviou": "send_document", "enviar": "send_document",
    "autorizou": "authorize_delivery",
}
_QUALIFIERS = {
    ("pretende",): "intention", ("pediu", "para"): "request",
    ("pediu", "pra"): "request", ("deve",): "obligation",
}
_DAY = {"amanhã", "hoje", "ontem"}


def words(text):
    return tuple(_TOKEN.findall(text.casefold()))


def object_words(text):
    return _nominal(words(text))


class EvidenceTarget(Contract):
    predicate: str = Field(min_length=1, max_length=48)
    object: str = Field(min_length=1, max_length=120)
    mode: Mode | Literal["obligation"] = "observation"
    polarity: Polarity = "affirmative"
    mention: str | None = Field(default=None, max_length=120)
    on_date: date | None = None
    local_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    condition: str | None = Field(default=None, max_length=4096)


class IdentityOption(Contract):
    """A lookup possibility from an authorized scope, with no canonical assertion."""

    mention: str = Field(min_length=1, max_length=120)
    actor_key: str = Field(min_length=1, max_length=120)


@dataclass(frozen=True)
class Statement:
    predicate: str
    object: tuple[str, ...]
    mention: tuple[str, ...] = ()
    mode: str = "observation"
    polarity: str = "affirmative"
    condition: tuple[str, ...] = ()
    on_date: date | None = None
    local_time: str | None = None


@dataclass(frozen=True)
class Assessment:
    state: str
    reason: str
    support: tuple[str, ...] = ()

    @property
    def clarification(self):
        if self.reason in {"UNRESOLVED_REFERENT", "AMBIGUOUS_IDENTITY", "INCOMPLETE_TIME", "CONDITION_NOT_SUPPORTED"}:
            return self.reason
        return None


def _nominal(tokens):
    return tokens[1:] if tokens and tokens[0] in _ARTICLES else tokens


def _arguments(tokens, anchors):
    """Read the object followed by an optional anchored day and local time."""
    tokens = _nominal(tokens)
    if not tokens or tokens[0] not in _OBJECTS:
        return None
    obj, rest = tokens[0], tokens[1:]
    if not rest:
        return (obj,), None, None
    if rest[:1] == ("para",):
        rest = rest[1:]
    if not rest or rest[0] not in _DAY:
        return None
    dates = {value for label, value in anchors.items() if words(label) == rest[:1]}
    on_date = next(iter(dates)) if len(dates) == 1 else None
    clock = rest[1:]
    if clock[:1] == (",",):
        clock = clock[1:]
    if not clock:
        return (obj,), on_date, None
    if clock[:1] != ("às",):
        return None
    hour, *tail = clock[1:] or ("",)
    if len(hour) > 2 or not hour.isascii() or not hour.isdecimal() or int(hour) > 23:
        return None
    if tail in (["h"], ["hora"], ["horas"]):
        minute = "00"
    elif len(tail) == 2 and tail[0] == ":" and re.fullmatch(r"[0-5][0-9]", tail[1]):
        minute = tail[1]
    else:
        return None
    return (obj,), on_date, f"{int(hour):02d}:{minute}"


def _clause(tokens, anchors):
    if tokens[:4] == ("não", "sei", "de", "quem") and tokens[4:] in {("estavam", "falando"), ("falavam",)}:
        return Statement("lack_knowledge", ("referente",))
    if tokens[:5] == ("o", "documento", "está", "assinado", "por"):
        mention = tokens[5:]
        if len(mention) == 3 and len(mention[0]) == 1 and mention[1] == "." and mention[2].isalpha():
            return Statement("signature_mention", ("documento",), mention)
        if 1 <= len(mention) <= 2 and all(token.isalpha() for token in mention):
            return Statement("signature_mention", ("documento",), mention)
        return None

    if tokens[-4:-1] == ("foi", "confirmada", "por") and tokens[-1].isalpha() and tokens[-1] not in _DEICTIC:
        arguments = _arguments(tokens[:-4], anchors)
        if arguments is not None and arguments[0] == ("reunião",):
            obj, day, clock = arguments
            return Statement("confirm_meeting", obj, tokens[-1:], on_date=day, local_time=clock)
        return None

    if not tokens or not tokens[0].isalpha() or tokens[0] in {"não", "talvez", "se", *_ARTICLES, *_DEICTIC}:
        return None
    mention, rest = tokens[:1], tokens[1:]
    if rest[:2] == ("disse", "que"):
        inner = _clause(rest[2:], anchors)
        return replace(inner, mode="reported") if inner and inner.mode == "observation" else None
    if rest[:2] == ("ainda", "não"):
        rest = rest[1:]
    negative = rest[:1] == ("não",)
    if negative:
        rest = rest[1:]
    mode = "observation"
    for qualifier, meaning in _QUALIFIERS.items():
        if rest[:len(qualifier)] == qualifier:
            mode, rest = meaning, rest[len(qualifier):]
            break
    verb = rest[0] if rest else None
    predicate = _VERBS.get(verb)
    if predicate is None or (verb == "enviar") != (mode != "observation"):
        return None
    remainder = rest[1:]
    if predicate == "authorize_delivery":
        if remainder[:3] != ("o", "envio", "do"):
            return None
        remainder = remainder[3:]
    arguments = _arguments(remainder, anchors)
    if arguments is None:
        return None
    obj, day, clock = arguments
    meeting = predicate in {"confirm_meeting", "schedule_meeting", "hold_meeting"}
    if meeting != (obj == ("reunião",)):
        return None
    return Statement(predicate, obj, mention, mode, "negative" if negative else "affirmative", (), day, clock)


def read_statement(text, anchors):
    tokens = words(text)
    if tokens[-1:] == (".",):
        tokens = tokens[:-1]
    # Retain qualifiers and require complete consumption of the supported clause.
    if "se" in tokens:
        split = tokens.index("se")
        condition = tokens[split + 1:]
        if not condition or not all(token.isalpha() for token in condition) or "se" in condition:
            return None
        base = _clause(tokens[:split], anchors)
        return replace(base, mode="conditional", condition=condition) if base and base.mode == "observation" else None
    return _clause(tokens, anchors)


def _mismatch(target, statement):
    if statement.predicate != target.predicate or statement.object != object_words(target.object):
        return "TARGET_NOT_SUPPORTED"
    if target.mention is not None and statement.mention != words(target.mention):
        return "TARGET_NOT_SUPPORTED"
    if (target.on_date and statement.on_date is None) or (target.local_time and statement.local_time is None):
        return "INCOMPLETE_TIME"
    if (target.on_date and target.on_date != statement.on_date) or (
        target.local_time and target.local_time != statement.local_time
    ):
        return "TARGET_NOT_SUPPORTED"
    if statement.condition != words(target.condition or ""):
        return "CONDITION_NOT_SUPPORTED"
    if statement.mode != target.mode:
        return "MODALITY_MISMATCH"
    return None


def assess_evidence(target: EvidenceTarget, sources, *, identity_options=()):
    """Compare typed claims by scope. Names are mentions and may never resolve an actor here."""
    if not sources:
        return Assessment("INSUFFICIENT_EVIDENCE", "NO_EVIDENCE")
    readings = [(source.id, read_statement(source.text, source.anchors)) for source in sources]
    if target.predicate == "person_identity":
        mentions = {
            stmt.mention for _, stmt in readings if stmt and stmt.predicate == "signature_mention"
            and stmt.object == object_words(target.object) and (target.mention is None or stmt.mention == words(target.mention))
        }
        for mention in mentions:
            matches = {option.actor_key for option in identity_options if words(option.mention) == mention}
            if len(matches) > 1:
                return Assessment("INSUFFICIENT_EVIDENCE", "AMBIGUOUS_IDENTITY")
        return Assessment("INSUFFICIENT_EVIDENCE", "UNRESOLVED_REFERENT")
    relevant = []
    failures = set()
    for source_id, statement in readings:
        if statement is None:
            continue
        reason = _mismatch(target, statement)
        if reason is None:
            relevant.append((source_id, statement))
        else:
            failures.add(reason)
    polarities = {statement.polarity for _, statement in relevant}
    if len(polarities) > 1:
        by_slot = {}
        for _, statement in relevant:
            slot = statement.mention, statement.on_date, statement.local_time
            if all(slot):
                by_slot.setdefault(slot, set()).add(statement.polarity)
        conflicts = tuple(ref for ref, s in relevant
                          if len(by_slot.get((s.mention, s.on_date, s.local_time), ())) > 1)
        if conflicts:
            return Assessment("CONFLICT", "CONTRADICTORY_EVIDENCE", conflicts)
        reason = "UNRESOLVED_REFERENT" if len({s.mention for _, s in relevant}) > 1 else "INCOMPLETE_TIME"
        return Assessment("INSUFFICIENT_EVIDENCE", reason)
    if relevant:
        if target.polarity not in polarities:
            return Assessment("INSUFFICIENT_EVIDENCE", "POLARITY_MISMATCH")
        observation = target.predicate in {"lack_knowledge", "signature_mention"}
        return Assessment(
            "OBSERVED_PROPOSAL" if target.mode == "observation" else "QUALIFIED_ONLY",
            "SUPPORTED_OBSERVATION" if observation else "SUPPORTED_RELATION",
            tuple(ref for ref, _ in relevant),
        )
    for reason in ("INCOMPLETE_TIME", "CONDITION_NOT_SUPPORTED", "MODALITY_MISMATCH"):
        if reason in failures:
            return Assessment("INSUFFICIENT_EVIDENCE", reason)
    if any(tokens[-1:] != ("?",) and _DEICTIC.intersection(tokens) for tokens in map(words, (s.text for s in sources))):
        return Assessment("INSUFFICIENT_EVIDENCE", "UNRESOLVED_REFERENT")
    return Assessment("INSUFFICIENT_EVIDENCE", "TARGET_NOT_SUPPORTED")


def matches_projection(statement, projection):
    """Compare a model projection to an already checked source statement."""
    predicate = projection["predicate"]
    return bool(
        statement
        and (predicate == statement.predicate or (statement.predicate == "confirm_meeting" and predicate == "confirmed"))
        and statement.object == object_words(projection["object"])
        and (statement.mode, statement.polarity) == (projection["mode"], projection["polarity"])
        and statement.condition == words(projection.get("condition") or "")
        and (statement.on_date.isoformat() if statement.on_date else None) == projection.get("date")
    )
