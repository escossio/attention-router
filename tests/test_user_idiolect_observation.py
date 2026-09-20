from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from attention_router.application.memory import (
    ArchivedMessageInput,
    archive_message,
    ingest_message,
)
from attention_router.infrastructure.models import (
    MemoryCandidateRow,
    MemoryClaimRow,
)


def _archive(
    session,
    *,
    source_message_id: str,
    text: str,
    from_me: bool = False,
    direction: str = "INBOUND",
):
    row, created = archive_message(
        session,
        ArchivedMessageInput(
            source="synthetic",
            source_account="test",
            thread_key="idiolect-observation",
            thread_type="DIRECT",
            source_message_id=source_message_id,
            sender_key="owner-observed",
            sender_display_name="Owner",
            sent_at=datetime.now(timezone.utc),
            text=text,
            from_me=from_me,
            direction=direction,
            lineage_classification="ORGANIC",
        ),
    )
    assert created
    return row


def test_single_passive_language_observation_stays_low_confidence_candidate(session):
    row = _archive(
        session,
        source_message_id="passive-profanity-1",
        text="Porra, isso ficou rápido.",
    )

    claims = ingest_message(session, row.id)

    candidate = session.scalar(
        select(MemoryCandidateRow).where(
            MemoryCandidateRow.message_id == row.id,
            MemoryCandidateRow.predicate
            == "communication.observed.profanity_tolerance",
        )
    )
    assert claims == []
    assert candidate is not None
    assert candidate.confidence == 0.40
    assert candidate.eligibility == "LOW_CONFIDENCE"
    assert candidate.eligibility_reason == "insufficient_explicit_evidence"
    assert candidate.object["signal"] == "PROFANITY_PRESENT"
    assert candidate.object["direction"] == "USER_TO_ANDY_LANGUAGE"
    assert candidate.object["evidence_class"] == "OBSERVED"
    assert candidate.object["reuse_policy"] == "INTERPRET_ONLY"
    assert candidate.object["generalization_scope"] == "MESSAGE"
    assert candidate.object["generalization_confidence"] == 0.0
    assert session.scalar(
        select(func.count()).select_from(MemoryClaimRow)
    ) == 0


def test_passive_language_observation_is_not_learned_from_andy_output(session):
    row = _archive(
        session,
        source_message_id="passive-profanity-outbound",
        text="Porra, isso ficou rápido.",
        from_me=True,
        direction="OUTBOUND",
    )

    claims = ingest_message(session, row.id)

    assert claims == []
    assert session.scalar(
        select(func.count())
        .select_from(MemoryCandidateRow)
        .where(
            MemoryCandidateRow.predicate
            == "communication.observed.profanity_tolerance"
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(MemoryClaimRow)
    ) == 0
