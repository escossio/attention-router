from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from attention_router.application.memory import (
    ArchivedMessageInput,
    archive_message,
    ingest_message,
)
from attention_router.infrastructure.models import (
    MemoryCandidateRow,
    MemoryClaimRow,
    MemoryEvidenceRow,
)


def _archive(
    session,
    *,
    source_message_id: str,
    text: str,
    from_me: bool = False,
    direction: str = "INBOUND",
    sent_at: datetime | None = None,
    sender_key: str = "owner-observed",
):
    row, created = archive_message(
        session,
        ArchivedMessageInput(
            source="synthetic",
            source_account="test",
            thread_key="idiolect-observation",
            thread_type="DIRECT",
            source_message_id=source_message_id,
            sender_key=sender_key,
            sender_display_name="Owner",
            sent_at=sent_at or datetime.now(timezone.utc),
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



def test_three_distinct_observations_promote_one_bounded_recurrent_claim(session):
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    first = _archive(
        session,
        source_message_id="recurrence-1",
        text="Porra, ficou rápido.",
        sent_at=base,
    )
    second = _archive(
        session,
        source_message_id="recurrence-2",
        text="Caralho, isso ficou bom.",
        sent_at=base + timedelta(days=2),
    )
    third = _archive(
        session,
        source_message_id="recurrence-3",
        text="Merda, agora foi.",
        sent_at=base + timedelta(days=4),
    )

    assert ingest_message(session, first.id) == []
    assert ingest_message(session, second.id) == []
    assert session.scalar(
        select(func.count()).select_from(MemoryClaimRow)
    ) == 0

    claims = ingest_message(session, third.id)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.predicate == "communication.observed.profanity_tolerance"
    assert claim.source_quality == "REPEATED_OBSERVATION"
    assert claim.status == "ACTIVE"
    assert claim.staleness_class == "PERISHABLE"
    assert claim.confidence == 0.70
    assert claim.object_json["signal"] == "PROFANITY_PRESENT"
    assert claim.object_json["direction"] == "USER_TO_ANDY_LANGUAGE"
    assert claim.object_json["evidence_class"] == "OBSERVED"
    assert claim.object_json["reuse_policy"] == "INTERPRET_ONLY"
    assert claim.object_json["generalization_scope"] == "PERSON"
    assert claim.object_json["generalization_confidence"] == 0.25
    assert claim.context["promotion_rule"] == "RECURRENCE_V1"
    assert claim.context["observation_threshold"] == 3
    assert claim.context["observation_count"] == 3
    assert claim.valid_until is not None
    assert claim.valid_until > claim.last_observed_at
    assert session.scalar(
        select(func.count())
        .select_from(MemoryEvidenceRow)
        .where(MemoryEvidenceRow.claim_id == claim.id)
    ) == 3


def test_fourth_observation_reinforces_same_claim_without_duplication(session):
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        _archive(
            session,
            source_message_id=f"reinforce-{index}",
            text=text,
            sent_at=base + timedelta(days=index),
        )
        for index, text in enumerate(
            (
                "Porra, primeiro.",
                "Caralho, segundo.",
                "Merda, terceiro.",
                "Porra, quarto.",
            ),
            start=1,
        )
    ]

    first_claim = None
    for row in rows:
        claims = ingest_message(session, row.id)
        if claims:
            first_claim = first_claim or claims[0]

    assert first_claim is not None
    assert session.scalar(
        select(func.count())
        .select_from(MemoryClaimRow)
        .where(
            MemoryClaimRow.predicate
            == "communication.observed.profanity_tolerance"
        )
    ) == 1
    session.refresh(first_claim)
    assert first_claim.context["observation_count"] == 4
    assert session.scalar(
        select(func.count())
        .select_from(MemoryEvidenceRow)
        .where(MemoryEvidenceRow.claim_id == first_claim.id)
    ) == 4


def test_recurrence_does_not_mix_people_or_stale_observations(session):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    actor_a_1 = _archive(
        session,
        source_message_id="scope-a-1",
        text="Porra, um.",
        sent_at=base,
        sender_key="actor-a",
    )
    actor_a_2 = _archive(
        session,
        source_message_id="scope-a-2",
        text="Caralho, dois.",
        sent_at=base + timedelta(days=1),
        sender_key="actor-a",
    )
    actor_b = _archive(
        session,
        source_message_id="scope-b-1",
        text="Merda, outro ator.",
        sent_at=base + timedelta(days=2),
        sender_key="actor-b",
    )
    stale_a = _archive(
        session,
        source_message_id="scope-a-stale-window",
        text="Porra, muito depois.",
        sent_at=base + timedelta(days=40),
        sender_key="actor-a",
    )

    for row in (actor_a_1, actor_a_2, actor_b, stale_a):
        assert ingest_message(session, row.id) == []

    assert session.scalar(
        select(func.count()).select_from(MemoryClaimRow)
    ) == 0
