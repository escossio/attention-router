"""Isolated structured proposal contract, adapted from the earlier offline lab.

Sources and temporal anchors are supplied by the caller after authorization.
Model output only contains textual assertions and coordinates into that input.
"""

from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator


VERSION = 1
Mode = Literal[
    "observation", "proposal", "intention", "uncertain", "reported",
    "conditional", "cancellation", "request",
]
Polarity = Literal["affirmative", "negative"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceDocument(Contract):
    id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4096)
    observed_at: AwareDatetime
    anchors: dict[str, date] = Field(default_factory=dict, max_length=12)


class Span(Contract):
    source_id: str = Field(min_length=1, max_length=120)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=4096)


class Proposal(Contract):
    version: Literal[1]
    mode: Mode
    polarity: Polarity
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    evidence: Span
    object: Span
    condition: Span | None = None
    temporal_anchor: Span | None = None
    corroboration: tuple[Span, ...] = Field(default=(), max_length=4)

    @field_validator("version", mode="before")
    @classmethod
    def require_integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


class OfflineInterpreter(Protocol):
    """Local adapter only. The laboratory never creates a network client."""

    def propose(self, source: SourceDocument, related: tuple[SourceDocument, ...]) -> dict | None: ...


def verify_span(span: Span, documents: dict[str, SourceDocument]) -> None:
    document = documents.get(span.source_id)
    if document is None:
        raise ValueError("SEMANTIC_UNKNOWN_SOURCE")
    if not span.start < span.end <= len(document.text) or document.text[span.start:span.end] != span.quote:
        raise ValueError("SEMANTIC_SPAN_MISMATCH")


def validate_proposal(raw, source: SourceDocument, related: tuple[SourceDocument, ...] = ()):
    """Return a typed hypothesis, scope-free value and grounded time, or abstain."""
    if raw is None:
        return None
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("SEMANTIC_SCHEMA_INVALID") from exc
    documents = {document.id: document for document in (*related, source)}
    if len(documents) != len(related) + 1:
        raise ValueError("SEMANTIC_DUPLICATE_INPUT")
    if proposal.evidence.source_id != source.id:
        raise ValueError("SEMANTIC_PRIMARY_SOURCE_MISMATCH")
    verify_span(proposal.evidence, documents)
    for span in (proposal.object, proposal.condition, proposal.temporal_anchor):
        if span is None:
            continue
        verify_span(span, documents)
        if (
            span.source_id != source.id or span.start < proposal.evidence.start
            or span.end > proposal.evidence.end
        ):
            raise ValueError("SEMANTIC_COMPONENT_OUTSIDE_EVIDENCE")
    if (proposal.mode == "conditional") != (proposal.condition is not None):
        raise ValueError("SEMANTIC_CONDITION_UNGROUNDED")

    references = {source.id}
    for span in proposal.corroboration:
        verify_span(span, documents)
        if span.source_id in references:
            raise ValueError("SEMANTIC_DUPLICATE_SUPPORT")
        references.add(span.source_id)

    value = {"mode": proposal.mode, "polarity": proposal.polarity,
             "predicate": proposal.predicate, "object": proposal.object.quote}
    if proposal.condition:
        value["condition"] = proposal.condition.quote
    if proposal.temporal_anchor:
        anchored = source.anchors.get(proposal.temporal_anchor.quote)
        if anchored is None:
            raise ValueError("SEMANTIC_TEMPORAL_ANCHOR_UNPROVEN")
        value["date"] = anchored.isoformat()
    return proposal, value


def span_for_fixture(source: SourceDocument, quote: str) -> dict:
    """Coordinates for hand-authored fixture proposals, not an interpreter."""
    start = source.text.index(quote)
    return {"source_id": source.id, "start": start, "end": start + len(quote), "quote": quote}


def source_document(source_id: str, text: str, observed_at: datetime, anchors=None) -> SourceDocument:
    return SourceDocument(id=source_id, text=text, observed_at=observed_at, anchors=anchors or {})
