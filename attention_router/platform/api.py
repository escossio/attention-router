from __future__ import annotations

import os
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from attention_router.config import settings
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.findings import FindingStatus, list_findings
from attention_router.platform.operations import (
    RuntimeProvenance,
    list_current_readiness,
    list_dependency_records,
    list_recent_observations,
    platform_snapshot,
)


SessionDependency = Callable[[], Generator[Session, None, None]]
AdminDependency = Callable[..., None]
GateStateProvider = Callable[[], Mapping[str, bool | None]]


def _environment_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def current_gate_states() -> Mapping[str, bool | None]:
    return {
        "AUTONOMOUS_EXECUTION_ENABLED": settings.autonomous_execution_enabled,
        "EXTERNAL_DELIVERY_ENABLED": settings.external_delivery_enabled,
        "TRANSPORT_EXTERNAL_DELIVERY_ENABLED": _environment_bool(
            "TRANSPORT_EXTERNAL_DELIVERY_ENABLED"
        ),
        "SYNTHETIC_TEST_DRIVER_ENABLED": getattr(
            settings, "synthetic_test_driver_enabled", False
        ),
    }


def read_operations_snapshot(
    session: Session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    gate_state_provider: GateStateProvider = current_gate_states,
) -> dict[str, Any]:
    """Read one sanitized snapshot without introducing a second data-plane contract."""

    revision = session.scalar(text("select version_num from alembic_version limit 1"))
    runtime_revision = os.environ.get("RUNTIME_HEAD", "unknown")
    source_revision = os.environ.get("SOURCE_REVISION", runtime_revision)
    observed_at = datetime.now(UTC)
    freshness_seconds = getattr(settings, "runtime_provenance_stale_after", 300)
    return platform_snapshot(
        session,
        tenant_id=tenant_id,
        runtime_provenance=RuntimeProvenance(
            source_revision=source_revision,
            runtime_revision=runtime_revision,
            schema_revision=revision or "unknown",
            observed_at=observed_at,
            fresh_until=observed_at + timedelta(seconds=freshness_seconds),
        ),
        gate_states=dict(gate_state_provider()),
        now=observed_at,
    )


def build_operations_router(
    *,
    get_session: SessionDependency,
    require_admin: AdminDependency,
    gate_state_provider: GateStateProvider = current_gate_states,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/platform/operations",
        tags=["platform-operations"],
        dependencies=[Depends(require_admin)],
    )
    @router.get("/snapshot")
    def snapshot(
        session: Session = Depends(get_session),
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> dict[str, Any]:
        return read_operations_snapshot(
            session,
            tenant_id=tenant_id,
            gate_state_provider=gate_state_provider,
        )

    @router.get("/dependencies")
    def dependencies(
        session: Session = Depends(get_session),
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[dict[str, Any]]:
        return list_dependency_records(session, tenant_id=tenant_id)

    @router.get("/readiness")
    def readiness(
        session: Session = Depends(get_session),
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[dict[str, Any]]:
        return list_current_readiness(session, tenant_id=tenant_id)

    @router.get("/observations")
    def observations(
        session: Session = Depends(get_session),
        tenant_id: str = DEFAULT_TENANT_ID,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        return list_recent_observations(session, tenant_id=tenant_id, limit=limit)

    @router.get("/findings")
    def findings(
        session: Session = Depends(get_session),
        tenant_id: str = DEFAULT_TENANT_ID,
        status: list[FindingStatus] | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return list_findings(session, tenant_id=tenant_id, statuses=status)

    return router
