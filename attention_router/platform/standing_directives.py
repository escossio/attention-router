"""Deterministic, tenant-scoped Standing Directive resolution."""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import StandingDirectiveRow
from attention_router.infrastructure.repository import audit
from attention_router.domain.models import new_id, now_utc


def create_standing_directive(session: Session, *, tenant_id: str, subject_actor_id: str,
                              created_by_actor_id: str, trigger_type: str, effect_type: str,
                              audience_selector: dict, provenance: str,
                              valid_from: datetime | None = None,
                              expires_at: datetime | None = None) -> StandingDirectiveRow:
    stamp = now_utc()
    row = StandingDirectiveRow(
        id=new_id(), tenant_id=tenant_id, subject_actor_id=subject_actor_id,
        created_by_actor_id=created_by_actor_id, trigger_type=trigger_type,
        effect_type=effect_type, audience_selector=audience_selector, status="ACTIVE",
        valid_from=valid_from or stamp, expires_at=expires_at, revoked_at=None,
        provenance=provenance, version=1, created_at=stamp, updated_at=stamp,
    )
    session.add(row)
    audit(
        session, None, "standing_directive_created",
        {"directive_id": row.id, "subject_actor_id": subject_actor_id,
         "trigger_type": trigger_type, "effect_type": effect_type,
         "audience_selector": audience_selector, "created_by_actor_id": created_by_actor_id},
        tenant_id=tenant_id,
    )
    session.flush()
    return row


def resolve_effective_standing_directives(session: Session, *, tenant_id: str,
                                          subject_actor_id: str, trigger_type: str,
                                          audience: str | None = None,
                                          relationship: str | None = None,
                                          now: datetime | None = None) -> list[StandingDirectiveRow]:
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = session.scalars(select(StandingDirectiveRow).where(
        StandingDirectiveRow.tenant_id == tenant_id,
        StandingDirectiveRow.subject_actor_id == subject_actor_id,
        StandingDirectiveRow.trigger_type == trigger_type,
        StandingDirectiveRow.status == "ACTIVE",
        StandingDirectiveRow.valid_from <= timestamp,
        StandingDirectiveRow.revoked_at.is_(None),
    )).all()
    result = []
    for row in rows:
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is not None and expires_at <= timestamp:
            continue
        selector = row.audience_selector or {}
        if selector.get("type") == "EVERYONE":
            result.append(row)
        elif selector.get("audience") is not None and selector.get("audience") == audience:
            result.append(row)
        elif selector.get("relationship") is not None and selector.get("relationship") == relationship:
            result.append(row)
    return sorted(result, key=lambda item: item.id)


def revoke_standing_directive(session: Session, directive_id: str, *, revoked_by: str) -> StandingDirectiveRow:
    row = session.get(StandingDirectiveRow, directive_id)
    if row is None:
        raise LookupError("STANDING_DIRECTIVE_NOT_FOUND")
    row.status = "REVOKED"
    row.revoked_at = now_utc()
    row.updated_at = now_utc()
    row.provenance = f"{row.provenance};revoked_by={revoked_by}"
    session.flush()
    return row
