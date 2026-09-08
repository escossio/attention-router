"""Persisted, least-privilege authorization for one bounded external run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    BoundedRunAuthorizationRow,
    EffectConsumptionRow,
)
from attention_router.infrastructure.repository import audit


class BoundedAuthorizationDenied(PermissionError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class BoundedAuthorizationResult:
    allowed: bool
    reason_code: str
    authorization_id: str | None = None
    remaining_budget: int = 0


@dataclass(frozen=True, slots=True)
class RequiredEffectAuthorization:
    """Exact authorization scope for one required external effect."""

    tenant_id: str
    scenario_run_id: str
    effect_budget_id: str
    level: str
    actor_scope: str
    target_scope: str
    capability_scope: str
    effect_scope: str


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def create_bounded_authorization(
    session: Session,
    *,
    tenant_id: str,
    scenario_run_id: str,
    effect_budget_id: str,
    level: str,
    actor_scope: str,
    target_scope: str,
    capability_scope: str,
    effect_scope: str,
    max_effects: int,
    authorized_by: str,
    correlation_id: str,
    valid_from: datetime,
    expires_at: datetime,
    provenance: dict | None = None,
    now: datetime | None = None,
) -> BoundedRunAuthorizationRow:
    if authorized_by in {"SYNTHETIC_ACTOR", "ANDY", "PROVIDER", "TRANSPORT"}:
        raise BoundedAuthorizationDenied("AUTHORITY_SOURCE_FORBIDDEN")
    if max_effects <= 0 or _utc(expires_at) <= _utc(valid_from):
        raise ValueError("INVALID_AUTHORIZATION_WINDOW_OR_BUDGET")
    timestamp = _utc(now)
    row = BoundedRunAuthorizationRow(
        id=str(uuid4()), tenant_id=tenant_id, scenario_run_id=scenario_run_id,
        effect_budget_id=effect_budget_id, level=level, actor_scope=actor_scope,
        target_scope=target_scope, capability_scope=capability_scope,
        effect_scope=effect_scope, max_effects=max_effects, authorized_by=authorized_by,
        status="ACTIVE",
        authorized_at=timestamp, valid_from=_utc(valid_from), expires_at=_utc(expires_at),
        correlation_id=correlation_id, provenance=provenance or {},
        created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    audit(
        session, None, "bounded_authorization_created",
        {"authorization_id": row.id, "scenario_run_id": scenario_run_id,
         "effect_budget_id": effect_budget_id, "level": level,
         "authorized_by": authorized_by},
        correlation_id=correlation_id, tenant_id=tenant_id,
    )
    session.flush()
    return row


def check_bounded_authorization(
    session: Session,
    *,
    tenant_id: str,
    scenario_run_id: str,
    effect_budget_id: str,
    level: str,
    actor_scope: str,
    target_scope: str,
    capability_scope: str,
    effect_scope: str,
    now: datetime | None = None,
) -> BoundedAuthorizationResult:
    row = session.scalar(select(BoundedRunAuthorizationRow).where(
        BoundedRunAuthorizationRow.tenant_id == tenant_id,
        BoundedRunAuthorizationRow.scenario_run_id == scenario_run_id,
        BoundedRunAuthorizationRow.effect_budget_id == effect_budget_id,
    ))
    if row is None:
        result = BoundedAuthorizationResult(False, "AUTHORIZATION_MISSING")
        audit(session, None, "bounded_authorization_denied", {"reason_code": result.reason_code}, tenant_id=tenant_id)
        return result
    timestamp = _utc(now)
    if row.status == "REVOKED":
        result = BoundedAuthorizationResult(False, "AUTHORIZATION_REVOKED", row.id)
        audit(session, None, "bounded_authorization_denied", {"authorization_id": row.id, "reason_code": result.reason_code}, correlation_id=row.correlation_id, tenant_id=tenant_id)
        return result
    if timestamp < _utc(row.valid_from) or timestamp >= _utc(row.expires_at):
        result = BoundedAuthorizationResult(False, "AUTHORIZATION_EXPIRED", row.id)
        audit(session, None, "bounded_authorization_denied", {"authorization_id": row.id, "reason_code": result.reason_code}, correlation_id=row.correlation_id, tenant_id=tenant_id)
        return result
    checks = (
        (row.level, level, "LEVEL_SCOPE_MISMATCH"),
        (row.actor_scope, actor_scope, "ACTOR_SCOPE_MISMATCH"),
        (row.target_scope, target_scope, "TARGET_SCOPE_MISMATCH"),
        (row.capability_scope, capability_scope, "CAPABILITY_SCOPE_MISMATCH"),
        (row.effect_scope, effect_scope, "EFFECT_SCOPE_MISMATCH"),
    )
    for actual, expected, reason in checks:
        if actual != expected:
            result = BoundedAuthorizationResult(False, reason, row.id)
            audit(session, None, "bounded_authorization_denied", {"authorization_id": row.id, "reason_code": reason}, correlation_id=row.correlation_id, tenant_id=tenant_id)
            return result
    consumed = session.scalar(select(func.count(EffectConsumptionRow.id)).where(
        EffectConsumptionRow.tenant_id == tenant_id,
        EffectConsumptionRow.effect_budget_id == effect_budget_id,
        EffectConsumptionRow.logical_effect_id == effect_scope,
    )) or 0
    remaining = row.max_effects - consumed
    if remaining <= 0 or row.status == "CONSUMED":
        result = BoundedAuthorizationResult(False, "EFFECT_BUDGET_EXHAUSTED", row.id, 0)
        audit(session, None, "bounded_authorization_denied", {"authorization_id": row.id, "reason_code": result.reason_code}, correlation_id=row.correlation_id, tenant_id=tenant_id)
        return result
    result = BoundedAuthorizationResult(True, "AUTHORIZED", row.id, remaining)
    audit(session, None, "bounded_authorization_resolved", {"authorization_id": row.id, "remaining_budget": remaining}, correlation_id=row.correlation_id, tenant_id=tenant_id)
    return result


def check_required_effect_authorization_coverage(
    session: Session,
    *,
    required: tuple[RequiredEffectAuthorization, ...],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Require exactly one valid, exact-scope authorization per effect budget."""

    timestamp = _utc(now)
    for scope in required:
        rows = session.scalars(select(BoundedRunAuthorizationRow).where(
            BoundedRunAuthorizationRow.tenant_id == scope.tenant_id,
            BoundedRunAuthorizationRow.scenario_run_id == scope.scenario_run_id,
            BoundedRunAuthorizationRow.effect_budget_id == scope.effect_budget_id,
        )).all()
        if len(rows) != 1:
            return False, "MISSING_REQUIRED_EFFECT_AUTHORIZATION" if not rows else "DUPLICATE_EFFECT_AUTHORIZATION"
        row = rows[0]
        if row.status == "REVOKED":
            return False, "AUTHORIZATION_REVOKED"
        if timestamp < _utc(row.valid_from) or timestamp >= _utc(row.expires_at):
            return False, "AUTHORIZATION_EXPIRED"
        actual = (
            row.level, row.actor_scope, row.target_scope,
            row.capability_scope, row.effect_scope,
        )
        expected = (
            scope.level, scope.actor_scope, scope.target_scope,
            scope.capability_scope, scope.effect_scope,
        )
        if actual != expected:
            return False, "AUTHORIZATION_SCOPE_MISMATCH"
    return True, "AUTHORIZED"


def revoke_bounded_authorization(session: Session, authorization_id: str, *, now=None) -> None:
    row = session.get(BoundedRunAuthorizationRow, authorization_id)
    if row is None:
        raise BoundedAuthorizationDenied("AUTHORIZATION_MISSING")
    row.status = "REVOKED"
    row.revoked_at = _utc(now)
    row.updated_at = _utc(now)
    audit(session, None, "bounded_authorization_revoked", {"authorization_id": row.id}, correlation_id=row.correlation_id, tenant_id=row.tenant_id)
    session.flush()
