"""Governed cross-source entity resolution for Personal Context V2C."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.entity_resolution_models import (
    EntityAliasResolutionRow,
    EntityResolutionCandidateRow,
    EntityResolutionEvidenceRow,
)
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    MemoryActorRow,
    TenantRow,
)


SUPPORTED_EVIDENCE_TYPES: Final = frozenset(
    {
        "EXACT_PROVIDER_IDENTITY",
        "NORMALIZED_PHONE",
        "NORMALIZED_EMAIL",
        "OWNER_CONFIRMATION",
        "SHARED_STABLE_IDENTIFIER",
        "EXPLICIT_RELATIONSHIP",
        "SOURCE_ALIAS",
    }
)


class EntityResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IdentityEvidenceInput:
    evidence_type: str
    source_ref: str
    confidence: float
    independence_key: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or now_utc()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _bounded(
    value: str,
    *,
    code: str,
    limit: int,
) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise EntityResolutionError(f"{code}_REQUIRED")
    if len(normalized) > limit:
        raise EntityResolutionError(f"{code}_TOO_LONG")
    return normalized


def _actor_exists(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> bool:
    binding = session.scalar(
        select(ActorBindingRow.id)
        .where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == actor_key,
        )
        .limit(1)
    )
    if binding is not None:
        return True
    memory_actor = session.scalar(
        select(MemoryActorRow.id)
        .where(
            MemoryActorRow.tenant_id == tenant_id,
            MemoryActorRow.actor_key == actor_key,
        )
        .limit(1)
    )
    return memory_actor is not None


def _canonical_pair(
    session: Session,
    *,
    tenant_id: str,
    actor_key_a: str,
    actor_key_b: str,
) -> tuple[str, str]:
    tenant = session.get(TenantRow, tenant_id)
    if tenant is None or tenant.status != "ACTIVE":
        raise EntityResolutionError("ENTITY_RESOLUTION_TENANT_UNAVAILABLE")

    first = _bounded(
        actor_key_a,
        code="ENTITY_RESOLUTION_ACTOR_A",
        limit=120,
    )
    second = _bounded(
        actor_key_b,
        code="ENTITY_RESOLUTION_ACTOR_B",
        limit=120,
    )
    if first == second:
        raise EntityResolutionError("ENTITY_RESOLUTION_SAME_ACTOR")

    for actor_key in (first, second):
        if not _actor_exists(
            session,
            tenant_id=tenant_id,
            actor_key=actor_key,
        ):
            raise EntityResolutionError("ENTITY_RESOLUTION_ACTOR_NOT_FOUND")

    return tuple(sorted((first, second)))


def _normalize_evidence(
    evidence: list[IdentityEvidenceInput],
) -> list[dict[str, Any]]:
    if not evidence:
        raise EntityResolutionError("ENTITY_RESOLUTION_EVIDENCE_REQUIRED")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        evidence_type = item.evidence_type.strip().upper()
        if evidence_type not in SUPPORTED_EVIDENCE_TYPES:
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_EVIDENCE_TYPE_UNSUPPORTED"
            )
        source_ref = _bounded(
            item.source_ref,
            code="ENTITY_RESOLUTION_SOURCE_REF",
            limit=240,
        )
        independence_key = _bounded(
            item.independence_key,
            code="ENTITY_RESOLUTION_INDEPENDENCE_KEY",
            limit=160,
        )
        if (
            isinstance(item.confidence, bool)
            or not isinstance(item.confidence, (int, float))
            or item.confidence < 0
            or item.confidence > 1
        ):
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_EVIDENCE_CONFIDENCE_INVALID"
            )

        key = (evidence_type, source_ref, independence_key)
        if key in seen:
            continue
        seen.add(key)

        metadata = dict(item.metadata or {})
        if len(metadata) > 32:
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_EVIDENCE_METADATA_TOO_LARGE"
            )
        normalized.append(
            {
                "evidence_type": evidence_type,
                "source_ref": source_ref,
                "independence_key": independence_key,
                "confidence": float(item.confidence),
                "metadata": metadata,
            }
        )

    if not normalized:
        raise EntityResolutionError("ENTITY_RESOLUTION_EVIDENCE_REQUIRED")

    return sorted(
        normalized,
        key=lambda item: (
            item["evidence_type"],
            item["source_ref"],
            item["independence_key"],
        ),
    )


def _evidence_confidence(evidence: list[dict[str, Any]]) -> float:
    """Average the strongest signal from each independent evidence family."""
    by_family: dict[str, float] = {}
    for item in evidence:
        key = item["independence_key"]
        by_family[key] = max(
            by_family.get(key, 0.0),
            float(item["confidence"]),
        )
    return round(sum(by_family.values()) / len(by_family), 6)


def _require_owner_decision(
    session: Session,
    *,
    tenant_id: str,
    decision_actor_key: str,
) -> str:
    actor_key = _bounded(
        decision_actor_key,
        code="ENTITY_RESOLUTION_DECISION_ACTOR",
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
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_OWNER_AMBIGUOUS"
        )
    if actor_key not in owner_actor_keys:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_OWNER_AUTHORITY_REQUIRED"
        )
    return actor_key


def propose_entity_resolution(
    session: Session,
    *,
    tenant_id: str,
    actor_key_a: str,
    actor_key_b: str,
    evidence: list[IdentityEvidenceInput],
    now: datetime | None = None,
) -> tuple[EntityResolutionCandidateRow, bool]:
    """Persist one deterministic candidate; never merge identities."""
    left, right = _canonical_pair(
        session,
        tenant_id=tenant_id,
        actor_key_a=actor_key_a,
        actor_key_b=actor_key_b,
    )
    normalized = _normalize_evidence(evidence)
    fingerprint = stable_hash(
        {
            "schema_version": "entity-resolution-evidence-v0",
            "evidence": normalized,
        }
    )
    idempotency_key = (
        "entity-resolution:"
        + stable_hash(
            {
                "tenant_id": tenant_id,
                "left_actor_key": left,
                "right_actor_key": right,
                "evidence_fingerprint": fingerprint,
            }
        )[:96]
    )

    existing = session.scalar(
        select(EntityResolutionCandidateRow).where(
            EntityResolutionCandidateRow.idempotency_key
            == idempotency_key
        )
    )
    if existing is not None:
        return existing, False

    stamp = _utc(now)
    candidate = EntityResolutionCandidateRow(
        id=new_id(),
        tenant_id=tenant_id,
        left_actor_key=left,
        right_actor_key=right,
        evidence_fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        confidence=_evidence_confidence(normalized),
        evidence_count=len(normalized),
        state="PROPOSED",
        decision_kind=None,
        decision_actor_key=None,
        decision_ref=None,
        canonical_actor_key=None,
        created_at=stamp,
        updated_at=stamp,
        decided_at=None,
    )
    session.add(candidate)
    session.flush()

    for item in normalized:
        evidence_key = (
            "entity-resolution-evidence:"
            + stable_hash(
                {
                    "candidate_id": candidate.id,
                    "evidence_type": item["evidence_type"],
                    "source_ref": item["source_ref"],
                    "independence_key": item["independence_key"],
                }
            )[:88]
        )
        session.add(
            EntityResolutionEvidenceRow(
                id=new_id(),
                candidate_id=candidate.id,
                idempotency_key=evidence_key,
                evidence_type=item["evidence_type"],
                source_ref=item["source_ref"],
                independence_key=item["independence_key"],
                confidence=item["confidence"],
                metadata_json=item["metadata"],
                created_at=stamp,
            )
        )
    session.flush()
    return candidate, True


def mark_entity_resolution_ambiguous(
    session: Session,
    *,
    candidate_id: str,
    now: datetime | None = None,
) -> EntityResolutionCandidateRow:
    row = session.get(EntityResolutionCandidateRow, candidate_id)
    if row is None:
        raise EntityResolutionError("ENTITY_RESOLUTION_CANDIDATE_NOT_FOUND")
    if row.state == "AMBIGUOUS":
        return row
    if row.state != "PROPOSED":
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_CANDIDATE_NOT_AMBIGUOUSABLE"
        )
    row.state = "AMBIGUOUS"
    row.decision_kind = "INSUFFICIENT_OR_CONFLICTING_EVIDENCE"
    row.updated_at = _utc(now)
    session.flush()
    return row


def reject_entity_resolution(
    session: Session,
    *,
    candidate_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> EntityResolutionCandidateRow:
    row = session.get(EntityResolutionCandidateRow, candidate_id)
    if row is None:
        raise EntityResolutionError("ENTITY_RESOLUTION_CANDIDATE_NOT_FOUND")
    if row.state == "REJECTED":
        return row
    if row.state not in {"PROPOSED", "AMBIGUOUS"}:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_CANDIDATE_NOT_REJECTABLE"
        )
    owner_actor_key = _require_owner_decision(
        session,
        tenant_id=row.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="ENTITY_RESOLUTION_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    row.state = "REJECTED"
    row.decision_kind = "OWNER_REJECTED"
    row.decision_actor_key = owner_actor_key
    row.decision_ref = reference
    row.updated_at = stamp
    row.decided_at = stamp
    session.flush()
    return row


def _active_alias(
    session: Session,
    *,
    tenant_id: str,
    alias_actor_key: str,
) -> EntityAliasResolutionRow | None:
    return session.scalar(
        select(EntityAliasResolutionRow).where(
            EntityAliasResolutionRow.tenant_id == tenant_id,
            EntityAliasResolutionRow.alias_actor_key == alias_actor_key,
            EntityAliasResolutionRow.state == "ACTIVE",
        )
    )


def confirm_entity_resolution(
    session: Session,
    *,
    candidate_id: str,
    canonical_actor_key: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> tuple[EntityResolutionCandidateRow, EntityAliasResolutionRow]:
    row = session.get(EntityResolutionCandidateRow, candidate_id)
    if row is None:
        raise EntityResolutionError("ENTITY_RESOLUTION_CANDIDATE_NOT_FOUND")

    if row.state == "CONFIRMED":
        alias = session.scalar(
            select(EntityAliasResolutionRow).where(
                EntityAliasResolutionRow.candidate_id == row.id
            )
        )
        if alias is None:
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_CONFIRMED_ALIAS_MISSING"
            )
        if row.canonical_actor_key != canonical_actor_key:
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_CANONICAL_ACTOR_MISMATCH"
            )
        if alias.state != "ACTIVE":
            raise EntityResolutionError(
                "ENTITY_RESOLUTION_CONFIRMED_ALIAS_INACTIVE"
            )
        return row, alias

    if row.state not in {"PROPOSED", "AMBIGUOUS"}:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_CANDIDATE_NOT_CONFIRMABLE"
        )

    canonical = _bounded(
        canonical_actor_key,
        code="ENTITY_RESOLUTION_CANONICAL_ACTOR",
        limit=120,
    )
    pair = {row.left_actor_key, row.right_actor_key}
    if canonical not in pair:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_CANONICAL_ACTOR_OUTSIDE_PAIR"
        )
    alias_actor_key = next(iter(pair - {canonical}))

    owner_actor_key = _require_owner_decision(
        session,
        tenant_id=row.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="ENTITY_RESOLUTION_DECISION_REF",
        limit=240,
    )

    existing_alias = _active_alias(
        session,
        tenant_id=row.tenant_id,
        alias_actor_key=alias_actor_key,
    )
    if existing_alias is not None:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_ALIAS_ALREADY_ACTIVE"
        )

    canonical_as_alias = _active_alias(
        session,
        tenant_id=row.tenant_id,
        alias_actor_key=canonical,
    )
    if canonical_as_alias is not None:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_CANONICAL_IS_ALIAS"
        )

    alias_dependents = session.scalar(
        select(EntityAliasResolutionRow.id)
        .where(
            EntityAliasResolutionRow.tenant_id == row.tenant_id,
            EntityAliasResolutionRow.canonical_actor_key
            == alias_actor_key,
            EntityAliasResolutionRow.state == "ACTIVE",
        )
        .limit(1)
    )
    if alias_dependents is not None:
        raise EntityResolutionError(
            "ENTITY_RESOLUTION_ALIAS_HAS_DEPENDENTS"
        )

    stamp = _utc(now)
    row.state = "CONFIRMED"
    row.decision_kind = "OWNER_CONFIRMED"
    row.decision_actor_key = owner_actor_key
    row.decision_ref = reference
    row.canonical_actor_key = canonical
    row.updated_at = stamp
    row.decided_at = stamp

    alias = EntityAliasResolutionRow(
        id=new_id(),
        tenant_id=row.tenant_id,
        candidate_id=row.id,
        alias_actor_key=alias_actor_key,
        canonical_actor_key=canonical,
        state="ACTIVE",
        decision_actor_key=owner_actor_key,
        decision_ref=reference,
        revoked_by_actor_key=None,
        revocation_ref=None,
        created_at=stamp,
        updated_at=stamp,
        revoked_at=None,
    )
    session.add(alias)
    session.flush()
    return row, alias


def revoke_entity_alias(
    session: Session,
    *,
    alias_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> EntityAliasResolutionRow:
    row = session.get(EntityAliasResolutionRow, alias_id)
    if row is None:
        raise EntityResolutionError("ENTITY_ALIAS_NOT_FOUND")
    if row.state == "REVOKED":
        return row
    if row.state != "ACTIVE":
        raise EntityResolutionError("ENTITY_ALIAS_NOT_REVOCABLE")

    owner_actor_key = _require_owner_decision(
        session,
        tenant_id=row.tenant_id,
        decision_actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="ENTITY_RESOLUTION_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    row.state = "REVOKED"
    row.revoked_by_actor_key = owner_actor_key
    row.revocation_ref = reference
    row.updated_at = stamp
    row.revoked_at = stamp
    session.flush()
    return row


__all__ = [
    "EntityResolutionError",
    "IdentityEvidenceInput",
    "SUPPORTED_EVIDENCE_TYPES",
    "confirm_entity_resolution",
    "mark_entity_resolution_ambiguous",
    "propose_entity_resolution",
    "reject_entity_resolution",
    "revoke_entity_alias",
]
