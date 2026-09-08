"""Canonical handoff from one fresh readiness result to inert preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable
from attention_router.observability.readiness_diagnostics import (
    emit_readiness_diagnostic,
    new_readiness_attempt_id,
)

from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.infrastructure.models import ReadinessResultRow
from attention_router.platform.bounded_authorization import (
    RequiredEffectAuthorization,
    create_bounded_authorization,
)
from attention_router.platform.execution_safety import (
    provision_scenario_execution_safety_in_transaction,
)
from attention_router.platform.scenarios import ScenarioManifest, create_scenario_run


class ReadinessHandoffDenied(PermissionError):
    """Raised when readiness cannot safely cross into inert preparation."""


@dataclass(frozen=True, slots=True)
class ReadinessHandoffRequest:
    tenant_id: str
    run_id: str
    scenario_version_id: str
    synthetic_actor_binding_id: str
    source_sha: str
    runtime_sha: str
    schema_revision: str
    driver_revision: str
    level: str
    actor_scope: str
    target_scope: str
    capability_scope: str
    effect_scope: str
    authorized_by: str
    # The correlation is the durable idempotency identity.  It is required so
    # retries cannot silently become a second logical preparation.
    root_correlation_id: str
    authorization_valid_from: datetime | None = None
    authorization_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadinessHandoffResult:
    readiness_result_id: str
    scenario_run_id: str
    effect_budget_id: str
    execution_lease_id: str
    bounded_authorization_id: str
    bounded_authorization_ids: tuple[str, ...] = ()


ReadinessRunner = Callable[[Session], ReadinessResultRow]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now(value: datetime | None) -> datetime:
    return _utc(value or datetime.now(UTC))


def prepare_from_fresh_readiness(
    session: Session,
    *,
    manifest: ScenarioManifest,
    request: ReadinessHandoffRequest,
    reevaluate_once: ReadinessRunner,
    now: datetime | None = None,
) -> ReadinessHandoffResult:
    """Reevaluate once, then atomically prepare an inert run.

    The readiness runner owns the canonical collect/evaluate/persist path and
    must return the newly persisted domain result.  Readiness persistence and
    preparation are deliberately separate transactions: a preparation
    failure must not erase readiness evidence, while the preparation set is
    all-or-none.  External observations are never treated as an ACID lock.
    """
    attempt_id = new_readiness_attempt_id()
    attempt_started_at = _now(now)
    readiness = None
    try:
        readiness = reevaluate_once(session)
    finally:
        evaluation = getattr(readiness, "_readiness_evaluation", None)
        emit_readiness_diagnostic(
            attempt_id=attempt_id,
            correlation_id=request.root_correlation_id,
            scenario_id=getattr(getattr(manifest, "scenario", None), "id", "unknown"),
            execution_level=request.level,
            attempt_started_at=attempt_started_at,
            evaluation=evaluation,
            handoff_result=("READINESS_NOT_READY" if readiness is not None and readiness.state != "READY" else None),
        )
    if (
        readiness.tenant_id != request.tenant_id
        or readiness.dimension != "DOMAIN_READINESS"
        or not readiness.is_current
    ):
        raise ReadinessHandoffDenied("READINESS_PROVENANCE_INVALID")
    if readiness.state != "READY":
        raise ReadinessHandoffDenied("READINESS_NOT_READY")
    if readiness.evidence_fresh_until is None:
        raise ReadinessHandoffDenied("READINESS_EVIDENCE_MISSING")
    # This clock sample is deliberately taken after the canonical evaluator
    # returns and immediately before the first preparation mutation.
    timestamp = _now(now)
    fresh_until = _utc(readiness.evidence_fresh_until)
    if timestamp >= fresh_until:
        raise ReadinessHandoffDenied("READINESS_EXPIRED_BEFORE_PREPARATION")
    if (timestamp - _utc(readiness.evaluated_at)).total_seconds() > settings.readiness_result_max_age:
        raise ReadinessHandoffDenied("READINESS_RESULT_STALE_BEFORE_PREPARATION")
    auth_valid_from = timestamp
    auth_expires_at = auth_valid_from + timedelta(
        seconds=settings.synthetic_l1_bounded_auth_validity_seconds
    )
    if (
        request.authorization_valid_from is not None
        and _utc(request.authorization_valid_from) != auth_valid_from
    ) or (
        request.authorization_expires_at is not None
        and _utc(request.authorization_expires_at) != auth_expires_at
    ):
        raise ReadinessHandoffDenied("BOUNDED_AUTH_VALIDITY_OVERRIDE_FORBIDDEN")

    correlation_id = request.root_correlation_id
    run_expires_at = auth_expires_at
    try:
        run = create_scenario_run(
            session,
            run_id=request.run_id,
            tenant_id=request.tenant_id,
            scenario_version_id=request.scenario_version_id,
            synthetic_actor_binding_id=request.synthetic_actor_binding_id,
            root_correlation_id=correlation_id,
            source_sha=request.source_sha,
            runtime_sha=request.runtime_sha,
            schema_revision=request.schema_revision,
            driver_revision=request.driver_revision,
            readiness_result_id=readiness.id,
            effect_budget_id=None,
            expires_at=run_expires_at,
            require_synthetic_actor=True,
            now=timestamp,
        )
        safety = provision_scenario_execution_safety_in_transaction(
            session,
            scenario_run_id=run.id,
            manifest=manifest,
            readiness_max_age_seconds=settings.readiness_result_max_age,
            lease_ttl_seconds=settings.scenario_step_lease_ttl,
            now=timestamp,
        )
        auth_specs = [RequiredEffectAuthorization(
            tenant_id=request.tenant_id, scenario_run_id=run.id,
            effect_budget_id=safety.system_budget_id, level=request.level,
            actor_scope=request.actor_scope, target_scope=request.target_scope,
            capability_scope=request.capability_scope, effect_scope=request.effect_scope,
        )]
        if getattr(safety, "stimulus_budget_id", None) and getattr(safety, "stimulus_lease_id", None):
            auth_specs.append(RequiredEffectAuthorization(
                tenant_id=request.tenant_id, scenario_run_id=run.id,
                effect_budget_id=safety.stimulus_budget_id, level=request.level,
                actor_scope=request.actor_scope, target_scope=request.target_scope,
                capability_scope="synthetic_send_bounded", effect_scope="WHATSAPP_STIMULUS",
            ))
        auths = [create_bounded_authorization(
            session, tenant_id=spec.tenant_id, scenario_run_id=spec.scenario_run_id,
            effect_budget_id=spec.effect_budget_id, level=spec.level,
            actor_scope=spec.actor_scope, target_scope=spec.target_scope,
            capability_scope=spec.capability_scope, effect_scope=spec.effect_scope,
            max_effects=1, authorized_by=request.authorized_by,
            correlation_id=correlation_id, valid_from=auth_valid_from,
            expires_at=auth_expires_at,
            provenance={"readiness_result_id": readiness.id, "handoff": "inert_preparation",
                        "required_external_effect": spec.effect_scope}, now=timestamp,
        ) for spec in auth_specs]
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ReadinessHandoffResult(
        readiness_result_id=readiness.id,
        scenario_run_id=run.id,
        effect_budget_id=safety.system_budget_id,
        execution_lease_id=safety.system_lease_id,
        bounded_authorization_id=auths[0].id,
        bounded_authorization_ids=tuple(auth.id for auth in auths),
    )


__all__ = ["ReadinessHandoffDenied", "ReadinessHandoffRequest", "ReadinessHandoffResult", "prepare_from_fresh_readiness"]
