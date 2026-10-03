"""Governed Candidate Insight boundary for Personal Context V2E."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.candidate_insight_models import (
    CandidateInsightEvidenceRow,
    CandidateInsightRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ConversationMessageRow,
    MemoryActorRow,
    RelationshipRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.semantic_episode_models import (
    SemanticEpisodeMembershipRow,
    SemanticEpisodeRow,
)


SUPPORTED_ENGINES: Final = {"RULE", "LLM", "EMBEDDING"}
SUPPORTED_INSIGHT_TYPES: Final = {
    "CLAIM_PROPOSAL",
    "RELATIONSHIP_PROPOSAL",
    "STATE_PROPOSAL",
}
SUPPORTED_EVIDENCE_TYPES: Final = {
    "SEMANTIC_EPISODE",
    "TIMELINE_EVENT",
    "CONVERSATION_MESSAGE",
}
_SENSITIVITY_ORDER: Final = {"NORMAL": 0, "PRIVATE": 1, "SECRET": 2}


class CandidateInsightError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateEvidenceInput:
    evidence_type: str
    source_ref: str
    independence_key: str
    confidence: float
    evidence_role: str = "SUPPORT"
    provenance: dict | None = None


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or now_utc()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _bounded(value: str, *, code: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise CandidateInsightError(f"{code}_REQUIRED")
    if len(normalized) > limit:
        raise CandidateInsightError(f"{code}_TOO_LONG")
    return normalized


def _confidence(value: float, *, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or value > 1
    ):
        raise CandidateInsightError(f"{code}_INVALID")
    return float(value)


def _sensitivity(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _SENSITIVITY_ORDER:
        raise CandidateInsightError("CANDIDATE_INSIGHT_SENSITIVITY_INVALID")
    return normalized


def _max_sensitivity(first: str, second: str) -> str:
    left = _sensitivity(first)
    right = _sensitivity(second)
    return max(
        (left, right),
        key=lambda item: _SENSITIVITY_ORDER[item],
    )


def _event_sensitivity(event: TimelineEventRow) -> str:
    visibility = (event.visibility or "").strip().upper()
    if visibility == "SECRET":
        return "SECRET"
    if visibility in {"PRIVATE", "OWNER_PRIVATE"}:
        return "PRIVATE"
    if visibility in {"NORMAL", "PUBLIC"}:
        return "NORMAL"
    return "SECRET"


def _subject_exists(
    session: Session,
    *,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
) -> bool:
    kind = subject_type.strip().upper()
    if kind == "RESOURCE":
        row = session.get(ResourceRow, subject_id)
        return bool(row and row.tenant_id == tenant_id)
    if kind == "RELATIONSHIP":
        row = session.get(RelationshipRow, subject_id)
        return bool(row and row.tenant_id == tenant_id)
    if kind in {"ACTOR", "PERSON"}:
        binding = session.scalar(
            select(ActorBindingRow.id).where(
                ActorBindingRow.tenant_id == tenant_id,
                or_(
                    ActorBindingRow.actor_key == subject_id,
                    ActorBindingRow.id == subject_id,
                ),
            )
        )
        memory_actor = session.scalar(
            select(MemoryActorRow.id).where(
                MemoryActorRow.tenant_id == tenant_id,
                or_(
                    MemoryActorRow.actor_key == subject_id,
                    MemoryActorRow.id == subject_id,
                ),
            )
        )
        return bool(binding or memory_actor)
    return False


def _require_owner_decision(
    session: Session,
    *,
    tenant_id: str,
    decision_actor_key: str,
) -> str:
    actor_key = _bounded(
        decision_actor_key,
        code="CANDIDATE_INSIGHT_DECISION_ACTOR",
        limit=120,
    )
    bindings = list(
        session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"]
                    .as_boolean()
                    .is_(True),
                ),
            )
        ).all()
    )
    owner_actor_keys = {binding.actor_key for binding in bindings}
    if len(owner_actor_keys) != 1:
        raise CandidateInsightError("CANDIDATE_INSIGHT_OWNER_AMBIGUOUS")
    if actor_key not in owner_actor_keys:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_OWNER_AUTHORITY_REQUIRED"
        )
    return actor_key


def _source_evidence(
    session: Session,
    *,
    tenant_id: str,
    evidence_type: str,
    source_ref: str,
) -> tuple[datetime, str]:
    if evidence_type == "SEMANTIC_EPISODE":
        row = session.get(SemanticEpisodeRow, source_ref)
        if row is None:
            raise CandidateInsightError("CANDIDATE_INSIGHT_EPISODE_NOT_FOUND")
        if row.tenant_id != tenant_id:
            raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_MISMATCH")
        return _utc(row.last_activity_at), _sensitivity(row.sensitivity_class)

    if evidence_type == "TIMELINE_EVENT":
        row = session.get(TimelineEventRow, source_ref)
        if row is None:
            raise CandidateInsightError("CANDIDATE_INSIGHT_EVENT_NOT_FOUND")
        if row.tenant_id != tenant_id:
            raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_MISMATCH")
        return _utc(row.occurred_at), _event_sensitivity(row)

    if evidence_type == "CONVERSATION_MESSAGE":
        row = session.get(ConversationMessageRow, source_ref)
        if row is None:
            raise CandidateInsightError("CANDIDATE_INSIGHT_MESSAGE_NOT_FOUND")
        if row.tenant_id != tenant_id:
            raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_MISMATCH")
        return _utc(row.sent_at), _sensitivity(row.sensitivity_class)

    raise CandidateInsightError("CANDIDATE_INSIGHT_EVIDENCE_TYPE_UNSUPPORTED")


def _normalize_evidence(
    session: Session,
    *,
    tenant_id: str,
    evidence: list[CandidateEvidenceInput],
) -> tuple[list[dict], str, list[str]]:
    if not evidence:
        raise CandidateInsightError("CANDIDATE_INSIGHT_EVIDENCE_REQUIRED")

    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    max_sensitivity = "NORMAL"
    contradiction_refs: list[str] = []
    support_count = 0

    for item in evidence:
        evidence_type = item.evidence_type.strip().upper()
        if evidence_type not in SUPPORTED_EVIDENCE_TYPES:
            raise CandidateInsightError(
                "CANDIDATE_INSIGHT_EVIDENCE_TYPE_UNSUPPORTED"
            )
        source_ref = _bounded(
            item.source_ref,
            code="CANDIDATE_INSIGHT_EVIDENCE_REF",
            limit=240,
        )
        independence_key = _bounded(
            item.independence_key,
            code="CANDIDATE_INSIGHT_INDEPENDENCE_KEY",
            limit=160,
        )
        role = item.evidence_role.strip().upper()
        if role not in {"SUPPORT", "CONTRADICTION"}:
            raise CandidateInsightError(
                "CANDIDATE_INSIGHT_EVIDENCE_ROLE_INVALID"
            )
        key = (evidence_type, source_ref, role)
        if key in seen:
            raise CandidateInsightError(
                "CANDIDATE_INSIGHT_EVIDENCE_DUPLICATE"
            )
        seen.add(key)

        observed_at, source_sensitivity = _source_evidence(
            session,
            tenant_id=tenant_id,
            evidence_type=evidence_type,
            source_ref=source_ref,
        )
        max_sensitivity = _max_sensitivity(
            max_sensitivity,
            source_sensitivity,
        )
        confidence = _confidence(
            item.confidence,
            code="CANDIDATE_INSIGHT_EVIDENCE_CONFIDENCE",
        )
        if role == "SUPPORT":
            support_count += 1
        else:
            contradiction_refs.append(source_ref)

        normalized.append(
            {
                "evidence_type": evidence_type,
                "source_ref": source_ref,
                "independence_key": independence_key,
                "evidence_role": role,
                "confidence": confidence,
                "sensitivity_class": source_sensitivity,
                "observed_at": observed_at,
                "provenance": item.provenance or {},
            }
        )

    if support_count == 0:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_SUPPORT_EVIDENCE_REQUIRED"
        )

    normalized.sort(
        key=lambda row: (
            row["evidence_role"],
            row["evidence_type"],
            row["source_ref"],
            row["independence_key"],
        )
    )
    return normalized, max_sensitivity, sorted(set(contradiction_refs))


def propose_candidate_insight(
    session: Session,
    *,
    tenant_id: str,
    insight_type: str,
    subject_type: str,
    subject_id: str,
    predicate: str,
    proposed_value: dict,
    source_engine: str,
    confidence: float,
    evidence: list[CandidateEvidenceInput],
    sensitivity_class: str = "NORMAL",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    provenance: dict | None = None,
    now: datetime | None = None,
) -> tuple[CandidateInsightRow, bool]:
    """Persist one candidate insight without creating canonical truth."""
    if session.get(TenantRow, tenant_id) is None:
        raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_NOT_FOUND")

    kind = insight_type.strip().upper()
    if kind not in SUPPORTED_INSIGHT_TYPES:
        raise CandidateInsightError("CANDIDATE_INSIGHT_TYPE_UNSUPPORTED")
    engine = source_engine.strip().upper()
    if engine not in SUPPORTED_ENGINES:
        raise CandidateInsightError("CANDIDATE_INSIGHT_ENGINE_UNSUPPORTED")

    subject_kind = subject_type.strip().upper()
    subject = _bounded(
        subject_id,
        code="CANDIDATE_INSIGHT_SUBJECT",
        limit=120,
    )
    if not _subject_exists(
        session,
        tenant_id=tenant_id,
        subject_type=subject_kind,
        subject_id=subject,
    ):
        raise CandidateInsightError("CANDIDATE_INSIGHT_SUBJECT_NOT_FOUND")

    relation = _bounded(
        predicate,
        code="CANDIDATE_INSIGHT_PREDICATE",
        limit=160,
    )
    if not isinstance(proposed_value, dict) or not proposed_value:
        raise CandidateInsightError("CANDIDATE_INSIGHT_VALUE_REQUIRED")

    score = _confidence(
        confidence,
        code="CANDIDATE_INSIGHT_CONFIDENCE",
    )
    normalized_evidence, evidence_sensitivity, contradiction_refs = (
        _normalize_evidence(
            session,
            tenant_id=tenant_id,
            evidence=evidence,
        )
    )
    candidate_sensitivity = _max_sensitivity(
        sensitivity_class,
        evidence_sensitivity,
    )

    start = _utc(valid_from) if valid_from is not None else None
    end = _utc(valid_until) if valid_until is not None else None
    if start is not None and end is not None and end < start:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_TEMPORAL_SCOPE_INVALID"
        )

    semantic_key = (
        "candidate-insight:"
        + stable_hash(
            {
                "tenant_id": tenant_id,
                "insight_type": kind,
                "subject_type": subject_kind,
                "subject_id": subject,
                "predicate": relation,
            }
        )[:96]
    )
    evidence_fingerprint = stable_hash(
        [
            {
                "evidence_type": row["evidence_type"],
                "source_ref": row["source_ref"],
                "independence_key": row["independence_key"],
                "evidence_role": row["evidence_role"],
                "confidence": row["confidence"],
            }
            for row in normalized_evidence
        ]
    )
    idempotency_key = (
        "candidate-insight:"
        + stable_hash(
            {
                "semantic_key": semantic_key,
                "proposed_value": proposed_value,
                "source_engine": engine,
                "confidence": score,
                "evidence_fingerprint": evidence_fingerprint,
                "valid_from": start.isoformat() if start else None,
                "valid_until": end.isoformat() if end else None,
            }
        )[:96]
    )

    existing = session.scalar(
        select(CandidateInsightRow).where(
            CandidateInsightRow.tenant_id == tenant_id,
            CandidateInsightRow.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False

    stamp = _utc(now)
    initial_state = (
        "PROPOSED"
        if engine == "RULE" and not contradiction_refs
        else "NEEDS_REVIEW"
    )
    candidate = CandidateInsightRow(
        id=new_id(),
        tenant_id=tenant_id,
        semantic_key=semantic_key,
        idempotency_key=idempotency_key,
        insight_type=kind,
        subject_type=subject_kind,
        subject_id=subject,
        predicate=relation,
        proposed_value=proposed_value,
        source_engine=engine,
        confidence=score,
        sensitivity_class=candidate_sensitivity,
        valid_from=start,
        valid_until=end,
        contradiction_refs=contradiction_refs,
        state=initial_state,
        supersedes_insight_id=None,
        decision_kind=None,
        decision_actor_key=None,
        decision_ref=None,
        provenance={
            **(provenance or {}),
            "evidence_fingerprint": evidence_fingerprint,
            "governance": "CANDIDATE_ONLY",
            "grants_authority": False,
            "materializes_canonical_truth": False,
        },
        created_at=stamp,
        updated_at=stamp,
        decided_at=None,
    )
    session.add(candidate)
    session.flush()

    for item in normalized_evidence:
        evidence_key = (
            "candidate-insight-evidence:"
            + stable_hash(
                {
                    "candidate_id": candidate.id,
                    "evidence_type": item["evidence_type"],
                    "source_ref": item["source_ref"],
                    "evidence_role": item["evidence_role"],
                }
            )[:88]
        )
        session.add(
            CandidateInsightEvidenceRow(
                id=new_id(),
                tenant_id=tenant_id,
                candidate_id=candidate.id,
                idempotency_key=evidence_key,
                evidence_type=item["evidence_type"],
                source_ref=item["source_ref"],
                independence_key=item["independence_key"],
                evidence_role=item["evidence_role"],
                confidence=item["confidence"],
                sensitivity_class=item["sensitivity_class"],
                observed_at=item["observed_at"],
                provenance=item["provenance"],
                created_at=stamp,
            )
        )
    session.flush()
    return candidate, True


def consolidate_episode_scope(
    session: Session,
    *,
    tenant_id: str,
    episode_id: str,
    now: datetime | None = None,
) -> tuple[CandidateInsightRow, bool]:
    """Create a deterministic candidate describing one governed episode scope."""
    episode = session.get(SemanticEpisodeRow, episode_id)
    if episode is None:
        raise CandidateInsightError("CANDIDATE_INSIGHT_EPISODE_NOT_FOUND")
    if episode.tenant_id != tenant_id:
        raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_MISMATCH")
    if episode.scope_type not in {"RESOURCE", "RELATIONSHIP"}:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_EPISODE_SCOPE_UNSUPPORTED"
        )

    memberships = list(
        session.scalars(
            select(SemanticEpisodeMembershipRow)
            .where(
                SemanticEpisodeMembershipRow.tenant_id == tenant_id,
                SemanticEpisodeMembershipRow.episode_id == episode.id,
                SemanticEpisodeMembershipRow.ambiguous.is_(False),
            )
            .order_by(
                SemanticEpisodeMembershipRow.observed_at,
                SemanticEpisodeMembershipRow.id,
            )
        ).all()
    )

    evidence = [
        CandidateEvidenceInput(
            evidence_type="SEMANTIC_EPISODE",
            source_ref=episode.id,
            independence_key=f"episode:{episode.id}",
            confidence=episode.confidence,
            provenance={"semantic_key": episode.semantic_key},
        )
    ]
    for membership in memberships:
        member_type = (
            "TIMELINE_EVENT"
            if membership.member_type == "TIMELINE_EVENT"
            else "CONVERSATION_MESSAGE"
        )
        evidence.append(
            CandidateEvidenceInput(
                evidence_type=member_type,
                source_ref=membership.member_ref,
                independence_key=f"episode-membership:{membership.id}",
                confidence=membership.confidence,
                provenance={
                    "episode_membership_id": membership.id,
                    "association_reason": membership.association_reason,
                    "association_source": membership.association_source,
                },
            )
        )

    return propose_candidate_insight(
        session,
        tenant_id=tenant_id,
        insight_type="CLAIM_PROPOSAL",
        subject_type=episode.scope_type,
        subject_id=episode.scope_ref,
        predicate="context.semantic_episode",
        proposed_value={
            "episode_id": episode.id,
            "episode_type": episode.episode_type,
            "episode_state": episode.state,
            "semantic_key": episode.semantic_key,
        },
        source_engine="RULE",
        confidence=episode.confidence,
        evidence=evidence,
        sensitivity_class=episode.sensitivity_class,
        valid_from=episode.started_at,
        valid_until=episode.ended_at,
        provenance={
            "consolidator": "EPISODE_SCOPE_V0",
            "source_episode_id": episode.id,
        },
        now=now,
    )


def admit_candidate_insight(
    session: Session,
    *,
    candidate_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> CandidateInsightRow:
    """Admit a candidate for governed downstream promotion; do not materialize it."""
    row = session.get(CandidateInsightRow, candidate_id)
    if row is None:
        raise CandidateInsightError("CANDIDATE_INSIGHT_NOT_FOUND")
    if row.state == "ADMITTED":
        return row
    if row.state not in {"PROPOSED", "NEEDS_REVIEW"}:
        raise CandidateInsightError("CANDIDATE_INSIGHT_NOT_ADMISSIBLE")
    if row.contradiction_refs:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_CONTRADICTION_REQUIRES_REVIEW"
        )

    owner = _require_owner_decision(
        session,
        tenant_id=row.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="CANDIDATE_INSIGHT_DECISION_REF",
        limit=240,
    )
    support = session.scalar(
        select(CandidateInsightEvidenceRow.id)
        .where(
            CandidateInsightEvidenceRow.candidate_id == row.id,
            CandidateInsightEvidenceRow.tenant_id == row.tenant_id,
            CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
        )
        .limit(1)
    )
    if support is None:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_SUPPORT_EVIDENCE_REQUIRED"
        )

    stamp = _utc(now)
    row.state = "ADMITTED"
    row.decision_kind = "OWNER_ADMITTED"
    row.decision_actor_key = owner
    row.decision_ref = reference
    row.updated_at = stamp
    row.decided_at = stamp
    session.flush()
    return row


def reject_candidate_insight(
    session: Session,
    *,
    candidate_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> CandidateInsightRow:
    row = session.get(CandidateInsightRow, candidate_id)
    if row is None:
        raise CandidateInsightError("CANDIDATE_INSIGHT_NOT_FOUND")
    if row.state == "REJECTED":
        return row
    if row.state not in {"PROPOSED", "NEEDS_REVIEW", "ADMITTED"}:
        raise CandidateInsightError("CANDIDATE_INSIGHT_NOT_REJECTABLE")

    owner = _require_owner_decision(
        session,
        tenant_id=row.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="CANDIDATE_INSIGHT_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    row.state = "REJECTED"
    row.decision_kind = "OWNER_REJECTED"
    row.decision_actor_key = owner
    row.decision_ref = reference
    row.updated_at = stamp
    row.decided_at = stamp
    session.flush()
    return row


def supersede_candidate_insight(
    session: Session,
    *,
    candidate_id: str,
    replacement_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> CandidateInsightRow:
    previous = session.get(CandidateInsightRow, candidate_id)
    replacement = session.get(CandidateInsightRow, replacement_id)
    if previous is None or replacement is None:
        raise CandidateInsightError("CANDIDATE_INSIGHT_NOT_FOUND")
    if previous.id == replacement.id:
        raise CandidateInsightError("CANDIDATE_INSIGHT_SUPERSESSION_CYCLE")
    if previous.tenant_id != replacement.tenant_id:
        raise CandidateInsightError("CANDIDATE_INSIGHT_TENANT_MISMATCH")
    if previous.semantic_key != replacement.semantic_key:
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_SUPERSESSION_KEY_MISMATCH"
        )
    if previous.state == "SUPERSEDED":
        if replacement.supersedes_insight_id != previous.id:
            raise CandidateInsightError(
                "CANDIDATE_INSIGHT_SUPERSESSION_MISMATCH"
            )
        return previous
    if previous.state == "REJECTED":
        raise CandidateInsightError(
            "CANDIDATE_INSIGHT_REJECTED_NOT_SUPERSEDEABLE"
        )

    owner = _require_owner_decision(
        session,
        tenant_id=previous.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="CANDIDATE_INSIGHT_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    previous.state = "SUPERSEDED"
    previous.decision_kind = "OWNER_SUPERSEDED"
    previous.decision_actor_key = owner
    previous.decision_ref = reference
    previous.updated_at = stamp
    previous.decided_at = stamp
    replacement.supersedes_insight_id = previous.id
    replacement.sensitivity_class = _max_sensitivity(
        replacement.sensitivity_class,
        previous.sensitivity_class,
    )
    replacement.updated_at = stamp
    session.flush()
    return previous


__all__ = [
    "CandidateEvidenceInput",
    "CandidateInsightError",
    "SUPPORTED_ENGINES",
    "SUPPORTED_EVIDENCE_TYPES",
    "SUPPORTED_INSIGHT_TYPES",
    "admit_candidate_insight",
    "consolidate_episode_scope",
    "propose_candidate_insight",
    "reject_candidate_insight",
    "supersede_candidate_insight",
]
