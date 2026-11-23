"""Bounded interpretation checks for the existing semantic lab."""

from datetime import date
from typing import Literal

from pydantic import Field

from andy_evidence_support import matches_projection, read_statement, words
from andy_semantic_contract import Contract, SourceDocument
from attention_router.infrastructure.hashing import stable_hash


class TargetInsufficiency(Contract):
    """A caller assessment bound to the source, outside the model proposal."""

    source_id: str = Field(min_length=1, max_length=120)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: Literal["UNRESOLVED_REFERENT", "AMBIGUOUS_IDENTITY", "INCOMPLETE_TIME"]

    def verify(self, source: SourceDocument) -> None:
        if self.source_id != source.id or self.source_sha256 != stable_hash(source.model_dump(mode="json")):
            raise ValueError("SEMANTIC_TARGET_SOURCE_MISMATCH")


class MeetingConfirmationQuery(Contract):
    relation: Literal["meeting_confirmation"]
    source_id: str = Field(min_length=1, max_length=120)
    subject_mention: str = Field(min_length=1, max_length=120)
    on_date: date | None = None
    local_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    def matches(self, proposal, source_id, text, span):
        if (
            self.on_date is None or self.local_time is None or self.source_id != source_id
            or span != [0, len(text)] or proposal.get("predicate") not in {"confirmed", "confirm_meeting"}
            or proposal.get("mode") != "observation" or proposal.get("polarity") != "affirmative"
            or proposal.get("condition") is not None or proposal.get("date") != self.on_date.isoformat()
        ):
            return False
        statement = read_statement(text, {"amanhã": self.on_date})
        return bool(
            statement and statement.predicate == "confirm_meeting" and statement.mention == words(self.subject_mention)
            and statement.local_time == self.local_time and matches_projection(statement, proposal)
        )
