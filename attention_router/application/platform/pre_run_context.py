"""Canonical, read-only context resolution before a scenario run exists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.platform.context import resolve_represented_subject
from attention_router.application.platform.entities import (
    EffectiveAudience,
    EffectiveRelationship,
    resolve_effective_audience,
    resolve_effective_relationship,
)
from attention_router.core.entities import EntityReference
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import ActorBindingRow


@dataclass(frozen=True, slots=True)
class CanonicalPreRunExecutionContext:
    """Tenant-scoped references resolved without creating execution state."""

    tenant_id: str
    scenario_id: str
    scenario_version: int
    synthetic_actor: EntityReference | None
    owner_actor: EntityReference | None
    configured_target_ref: str | None
    resolved_target: EntityReference | None
    relationship: EffectiveRelationship
    audience: EffectiveAudience
    observed_at: datetime
    reason_code: str

    @property
    def synthetic_actor_ready(self) -> bool:
        return self.synthetic_actor is not None

    @property
    def synthetic_actor_not_owner(self) -> bool:
        return bool(self.synthetic_actor and self.owner_actor and self.synthetic_actor != self.owner_actor)

    @property
    def owner_target_resolved(self) -> bool:
        return self.resolved_target is not None and self.resolved_target == self.owner_actor


def resolve_pre_run_execution_context(
    session: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    scenario_version: int,
    synthetic_actor_key: str,
    configured_target_ref: str = "OWNER",
    now: datetime | None = None,
) -> CanonicalPreRunExecutionContext:
    """Resolve actor, owner, target, relationship and audience using domain queries."""
    observed_at = (now or now_utc()).astimezone(UTC)
    actor = session.scalar(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.actor_key == synthetic_actor_key,
            ActorBindingRow.is_active.is_(True),
        )
    )
    synthetic = (
        EntityReference(entity_type="ACTOR", entity_id=actor.actor_key)
        if actor and actor.actor_category.upper() == "SYNTHETIC_TEST_ACTOR"
        else None
    )
    owner = resolve_represented_subject(session, tenant_id)
    target = owner if configured_target_ref == "OWNER" else None
    relationship = (
        resolve_effective_relationship(session, tenant_id=tenant_id, source=synthetic, target=target, now=observed_at)
        if synthetic and target
        else EffectiveRelationship(None, None, "UNKNOWN", "PRE_RUN_CONTEXT_INCOMPLETE")
    )
    audience = resolve_effective_audience(actor=actor if synthetic else None, relationship=relationship)
    reasons: list[str] = []
    if synthetic is None:
        reasons.append("SYNTHETIC_ACTOR_UNRESOLVED")
    if owner is None:
        reasons.append("OWNER_UNRESOLVED")
    if target is None:
        reasons.append("OWNER_TARGET_UNRESOLVED")
    if synthetic and owner and synthetic == owner:
        reasons.append("SYNTHETIC_ACTOR_IS_OWNER")
    return CanonicalPreRunExecutionContext(
        tenant_id=tenant_id, scenario_id=scenario_id, scenario_version=scenario_version,
        synthetic_actor=synthetic, owner_actor=owner, configured_target_ref=configured_target_ref,
        resolved_target=target, relationship=relationship, audience=audience,
        observed_at=observed_at, reason_code="OK" if not reasons else reasons[0],
    )
