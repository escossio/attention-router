"""Tenant-scoped owner Personal Context read model.

Personal Context is knowledge, not authority.  Nothing in this module grants
permission to disclose a value or execute an action.  It intentionally remains
read-only and independent from outbound execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    ActorBindingRow,
    FactRow,
    MemoryActorRow,
    MemoryClaimRow,
)


class PersonalContextUnavailable(RuntimeError):
    """Raised when the represented tenant owner cannot be resolved uniquely."""


@dataclass(frozen=True)
class PersonalContextClaim:
    claim_id: str
    predicate: str
    object_type: str
    value_text: str | None
    value_json: dict[str, Any] | None
    confidence: float
    sensitivity: str
    source_quality: str
    staleness_class: str
    valid_from: datetime | None
    valid_until: datetime | None
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True)
class PersonalContextFact:
    fact_id: str
    predicate: str
    value_json: dict[str, Any] | None
    value_ref: str | None
    fact_class: str
    source_type: str
    source_ref: str | None
    confidence: float
    observed_at: datetime
    valid_from: datetime | None
    valid_until: datetime | None


@dataclass(frozen=True)
class PersonalContextSnapshot:
    tenant_id: str
    represented_actor_id: str
    source_alias_count: int
    claims: tuple[PersonalContextClaim, ...]
    facts: tuple[PersonalContextFact, ...]
    generated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        """Return the internal read model without changing disclosure authority."""
        return asdict(self)


def _owner_bindings(session: Session, tenant_id: str) -> tuple[str, list[ActorBindingRow]]:
    bindings = list(
        session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
                ),
            )
        ).all()
    )
    actor_keys = {binding.actor_key for binding in bindings}
    if len(actor_keys) != 1:
        raise PersonalContextUnavailable("REPRESENTED_OWNER_NOT_UNIQUE")
    return next(iter(actor_keys)), bindings


def _active_at(valid_from: datetime | None, valid_until: datetime | None, now: datetime) -> bool:
    if valid_from is not None and valid_from > now:
        return False
    if valid_until is not None and valid_until <= now:
        return False
    return True


def build_personal_context(
    session: Session,
    tenant_id: str,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> PersonalContextSnapshot:
    """Build the represented owner's bounded Personal Context.

    The owner is resolved from tenant-scoped ActorBindings. Multiple provider
    bindings are allowed when they all resolve to the same canonical actor key.
    Memory aliases are resolved only inside the same tenant. Secret claims and
    inactive/expired claims are excluded fail-closed.
    """
    if limit < 1 or limit > 500:
        raise ValueError("PERSONAL_CONTEXT_LIMIT_OUT_OF_RANGE")
    stamp = now or datetime.now(timezone.utc)
    actor_key, bindings = _owner_bindings(session, tenant_id)
    aliases = {actor_key, *(binding.external_actor_id for binding in bindings)}

    memory_actors = list(
        session.scalars(
            select(MemoryActorRow).where(
                MemoryActorRow.tenant_id == tenant_id,
                MemoryActorRow.actor_key.in_(aliases),
            )
        ).all()
    )
    memory_actor_ids = [actor.id for actor in memory_actors]

    claims: list[PersonalContextClaim] = []
    if memory_actor_ids:
        rows = list(
            session.scalars(
                select(MemoryClaimRow)
                .where(
                    MemoryClaimRow.subject_actor_id.in_(memory_actor_ids),
                    MemoryClaimRow.status == "ACTIVE",
                    MemoryClaimRow.sensitivity_class != "SECRET",
                )
                .order_by(MemoryClaimRow.updated_at.desc())
                .limit(limit)
            ).all()
        )
        for row in rows:
            if not _active_at(row.valid_from, row.valid_until, stamp):
                continue
            claims.append(
                PersonalContextClaim(
                    claim_id=row.id,
                    predicate=row.predicate,
                    object_type=row.object_type,
                    value_text=row.object_text,
                    value_json=row.object_json,
                    confidence=row.confidence,
                    sensitivity=row.sensitivity_class,
                    source_quality=row.source_quality,
                    staleness_class=row.staleness_class,
                    valid_from=row.valid_from,
                    valid_until=row.valid_until,
                    first_observed_at=row.first_observed_at,
                    last_observed_at=row.last_observed_at,
                )
            )

    fact_rows = list(
        session.scalars(
            select(FactRow)
            .where(
                FactRow.tenant_id == tenant_id,
                FactRow.subject_type == "ACTOR",
                FactRow.subject_id == actor_key,
            )
            .order_by(FactRow.observed_at.desc())
            .limit(limit)
        ).all()
    )
    facts = tuple(
        PersonalContextFact(
            fact_id=row.id,
            predicate=row.predicate,
            value_json=row.value_json,
            value_ref=row.value_ref,
            fact_class=row.fact_class,
            source_type=row.source_type,
            source_ref=row.source_ref,
            confidence=row.confidence,
            observed_at=row.observed_at,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
        )
        for row in fact_rows
        if _active_at(row.valid_from, row.valid_until, stamp)
    )

    return PersonalContextSnapshot(
        tenant_id=tenant_id,
        represented_actor_id=actor_key,
        source_alias_count=len(aliases),
        claims=tuple(claims),
        facts=facts,
        generated_at=stamp,
    )
