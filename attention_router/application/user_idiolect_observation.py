from __future__ import annotations

from datetime import timedelta
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    MemoryCandidateRow,
    MemoryClaimRow,
    MemoryEvidenceRow,
)


PASSIVE_OBSERVATION_CONFIDENCE = 0.40
RECURRENCE_PROMOTION_THRESHOLD = 3
RECURRENCE_WINDOW = timedelta(days=30)
RECURRENT_CLAIM_TTL = timedelta(days=30)
RECURRENT_CLAIM_CONFIDENCE = 0.70
RECURRENT_GENERALIZATION_CONFIDENCE = 0.25

_PASSIVE_PROFANITY_RE = re.compile(
    r"(?i)\b(?:porra|caralho|merda)\b"
)


def detect_passive_idiolect_observations(
    text: str,
) -> tuple[tuple[str, dict[str, Any], float, str], ...]:
    """Return bounded passive language observations.

    These observations are intentionally low-confidence and must remain
    non-promotable on a single occurrence.
    """

    if not text.strip():
        return ()

    observations: list[tuple[str, dict[str, Any], float, str]] = []

    if _PASSIVE_PROFANITY_RE.search(text):
        observations.append(
            (
                "communication.observed.profanity_tolerance",
                {
                    "signal": "PROFANITY_PRESENT",
                    "direction": "USER_TO_ANDY_LANGUAGE",
                    "evidence_class": "OBSERVED",
                    "reuse_policy": "INTERPRET_ONLY",
                    "generalization_scope": "MESSAGE",
                    "generalization_confidence": 0.0,
                },
                PASSIVE_OBSERVATION_CONFIDENCE,
                "PASSIVE_OBSERVATION",
            )
        )

    return tuple(observations)


def _matching_passive_support(
    candidate: MemoryCandidateRow,
    *,
    actor_id: str,
    predicate: str,
    signal: str,
) -> bool:
    obj = candidate.object or {}
    return (
        candidate.subject.get("actor_id") == actor_id
        and candidate.predicate == predicate
        and candidate.eligibility == "LOW_CONFIDENCE"
        and obj.get("source_quality") == "PASSIVE_OBSERVATION"
        and obj.get("signal") == signal
        and obj.get("direction") == "USER_TO_ANDY_LANGUAGE"
        and obj.get("evidence_class") == "OBSERVED"
        and obj.get("reuse_policy") == "INTERPRET_ONLY"
    )


def _existing_recurrent_claim(
    session: Session,
    *,
    actor_id: str,
    predicate: str,
    signal: str,
) -> MemoryClaimRow | None:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == predicate,
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc())
    ).all()
    for row in rows:
        value = row.object_json or {}
        if (
            row.source_quality == "REPEATED_OBSERVATION"
            and value.get("signal") == signal
            and value.get("evidence_class") == "OBSERVED"
            and value.get("reuse_policy") == "INTERPRET_ONLY"
        ):
            return row
    return None


def promote_recurrent_passive_observation(
    session: Session,
    *,
    candidate: MemoryCandidateRow,
    message: ConversationMessageRow,
) -> MemoryClaimRow | None:
    """Promote repeated passive observations into a bounded MemoryClaim.

    A single observation never promotes. Promotion requires three distinct
    inbound observations from the same actor within a rolling 30-day window.
    The resulting claim remains INTERPRET_ONLY and expires unless reinforced.
    """

    actor_id = candidate.subject.get("actor_id")
    obj = candidate.object or {}
    signal = obj.get("signal")
    if (
        not isinstance(actor_id, str)
        or not actor_id
        or not isinstance(signal, str)
        or not signal
        or candidate.eligibility != "LOW_CONFIDENCE"
        or obj.get("source_quality") != "PASSIVE_OBSERVATION"
        or message.from_me
        or message.direction != "INBOUND"
    ):
        return None

    cutoff = message.sent_at - RECURRENCE_WINDOW
    raw_support = session.scalars(
        select(MemoryCandidateRow)
        .join(
            ConversationMessageRow,
            ConversationMessageRow.id == MemoryCandidateRow.message_id,
        )
        .where(
            ConversationMessageRow.tenant_id == message.tenant_id,
            ConversationMessageRow.sent_at >= cutoff,
            ConversationMessageRow.sent_at <= message.sent_at,
            ConversationMessageRow.direction == "INBOUND",
            ConversationMessageRow.from_me.is_(False),
            MemoryCandidateRow.predicate == candidate.predicate,
            MemoryCandidateRow.eligibility == "LOW_CONFIDENCE",
        )
        .order_by(ConversationMessageRow.sent_at, MemoryCandidateRow.id)
    ).all()

    support_by_message: dict[str, MemoryCandidateRow] = {}
    for item in raw_support:
        if _matching_passive_support(
            item,
            actor_id=actor_id,
            predicate=candidate.predicate,
            signal=signal,
        ):
            support_by_message.setdefault(item.message_id, item)

    support = tuple(support_by_message.values())
    if len(support) < RECURRENCE_PROMOTION_THRESHOLD:
        return None

    support_messages = [
        session.get(ConversationMessageRow, item.message_id) for item in support
    ]
    observed = [item for item in support_messages if item is not None]
    if len(observed) < RECURRENCE_PROMOTION_THRESHOLD:
        return None

    first_observed = min(item.sent_at for item in observed)
    last_observed = max(item.sent_at for item in observed)
    claim = _existing_recurrent_claim(
        session,
        actor_id=actor_id,
        predicate=candidate.predicate,
        signal=signal,
    )

    if claim is None:
        claim = MemoryClaimRow(
            id=new_id(),
            subject_actor_id=actor_id,
            subject_entity_id=None,
            predicate=candidate.predicate,
            object_type="JSON",
            object_text=None,
            object_actor_id=None,
            object_entity_id=None,
            object_json={
                "signal": signal,
                "direction": "USER_TO_ANDY_LANGUAGE",
                "evidence_class": "OBSERVED",
                "reuse_policy": "INTERPRET_ONLY",
                "generalization_scope": "PERSON",
                "generalization_confidence": RECURRENT_GENERALIZATION_CONFIDENCE,
            },
            context={
                "promotion_rule": "RECURRENCE_V1",
                "observation_threshold": RECURRENCE_PROMOTION_THRESHOLD,
                "window_days": RECURRENCE_WINDOW.days,
                "observation_count": len(support),
            },
            confidence=RECURRENT_CLAIM_CONFIDENCE,
            sensitivity_class=candidate.sensitivity_class,
            source_quality="REPEATED_OBSERVATION",
            valid_from=first_observed,
            valid_until=last_observed + RECURRENT_CLAIM_TTL,
            status="ACTIVE",
            staleness_class="PERISHABLE",
            supersedes_claim_id=None,
            conflict_group_id=None,
            first_observed_at=first_observed,
            last_observed_at=last_observed,
            created_at=now_utc(),
            updated_at=now_utc(),
        )
        session.add(claim)
        session.flush()
    else:
        claim.first_observed_at = min(claim.first_observed_at, first_observed)
        claim.last_observed_at = max(claim.last_observed_at, last_observed)
        claim.valid_until = claim.last_observed_at + RECURRENT_CLAIM_TTL
        claim.context = {
            **(claim.context or {}),
            "observation_count": len(support),
        }
        claim.updated_at = now_utc()

    for item in support:
        existing = session.scalar(
            select(MemoryEvidenceRow).where(
                MemoryEvidenceRow.claim_id == claim.id,
                MemoryEvidenceRow.conversation_message_id == item.message_id,
            )
        )
        if existing is None:
            session.add(
                MemoryEvidenceRow(
                    id=new_id(),
                    claim_id=claim.id,
                    conversation_message_id=item.message_id,
                    extraction_run_id=item.extraction_run_id,
                    evidence_type="REPEATED_LANGUAGE_OBSERVATION",
                    confidence=item.confidence,
                    created_at=now_utc(),
                )
            )

    return claim


__all__ = [
    "PASSIVE_OBSERVATION_CONFIDENCE",
    "RECURRENCE_PROMOTION_THRESHOLD",
    "RECURRENCE_WINDOW",
    "RECURRENT_CLAIM_TTL",
    "RECURRENT_CLAIM_CONFIDENCE",
    "RECURRENT_GENERALIZATION_CONFIDENCE",
    "detect_passive_idiolect_observations",
    "promote_recurrent_passive_observation",
]
