from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.platform.capability_lab_read_model import read_scenario_engine_snapshot


def build_capability_lab_router(*, get_session: Any, require_admin: Any) -> APIRouter:
    """Expose observation-only Capability Lab projections.

    This router deliberately contains no mutation endpoints.
    """

    router = APIRouter(
        prefix="/api/v1/admin/platform/capability-lab",
        tags=["capability-lab"],
        dependencies=[Depends(require_admin)],
    )

    @router.get("/scenario-engine")
    def scenario_engine_snapshot(
        tenant_id: str = DEFAULT_TENANT_ID,
        run_limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        return read_scenario_engine_snapshot(
            session,
            tenant_id=tenant_id,
            run_limit=run_limit,
        )

    return router
