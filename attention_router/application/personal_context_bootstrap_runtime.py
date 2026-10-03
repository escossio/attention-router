"""Default-off bounded worker cycle for Semantic Bootstrap Product Runtime V0."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.memory import HistoryAdapter
from attention_router.application.personal_context_bootstrap import (
    SUPPORTED_BOOTSTRAP_SOURCES,
    process_next_bootstrap_batch,
)
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PersonalContextBootstrapRuntimeResult:
    runs_considered: int = 0
    batches_completed: int = 0
    runs_completed: int = 0
    runs_requeued: int = 0
    runs_failed: int = 0


def run_personal_context_bootstrap_runtime_cycle(
    session: Session,
    *,
    adapter: HistoryAdapter | None = None,
    adapter_factory: Callable[
        [PersonalContextBootstrapRunRow], HistoryAdapter
    ] | None = None,
    source_kinds: set[str] | frozenset[str] | None = None,
    run_limit: int = 5,
    canary_tenant_id: str | None = None,
) -> PersonalContextBootstrapRuntimeResult:
    """Advance at most one durable batch per selected QUEUED run."""
    if run_limit < 1 or run_limit > 100:
        raise ValueError("PERSONAL_CONTEXT_BOOTSTRAP_RUN_LIMIT_OUT_OF_RANGE")
    if (adapter is None) == (adapter_factory is None):
        raise ValueError(
            "PERSONAL_CONTEXT_BOOTSTRAP_ADAPTER_SELECTION_INVALID"
        )

    selected_sources = frozenset(
        source_kinds
        if source_kinds is not None
        else (
            {"WHATSAPP_TEXT"}
            if adapter is not None
            else SUPPORTED_BOOTSTRAP_SOURCES
        )
    )
    if (
        not selected_sources
        or not selected_sources.issubset(SUPPORTED_BOOTSTRAP_SOURCES)
    ):
        raise ValueError(
            "PERSONAL_CONTEXT_BOOTSTRAP_SOURCE_SELECTION_INVALID"
        )

    query = select(PersonalContextBootstrapRunRow).where(
        PersonalContextBootstrapRunRow.state == "QUEUED",
        PersonalContextBootstrapRunRow.source_kind.in_(
            sorted(selected_sources)
        ),
    )
    if canary_tenant_id is not None:
        query = query.where(
            PersonalContextBootstrapRunRow.tenant_id == canary_tenant_id
        )
    rows = list(
        session.scalars(
            query.order_by(
                PersonalContextBootstrapRunRow.created_at,
                PersonalContextBootstrapRunRow.id,
            )
            .limit(run_limit)
            .with_for_update(skip_locked=True)
        ).all()
    )

    batches_completed = 0
    runs_completed = 0
    runs_requeued = 0
    runs_failed = 0

    for row in rows:
        try:
            selected_adapter = (
                adapter_factory(row)
                if adapter_factory is not None
                else adapter
            )
            if selected_adapter is None:
                raise ValueError(
                    "PERSONAL_CONTEXT_BOOTSTRAP_ADAPTER_UNAVAILABLE"
                )
            with session.begin_nested():
                result = process_next_bootstrap_batch(
                    session,
                    row.id,
                    adapter=selected_adapter,
                )
            if result.batch_id is not None and result.state != "FAILED":
                batches_completed += 1
            if result.state == "COMPLETED":
                runs_completed += 1
            elif result.state == "QUEUED":
                runs_requeued += 1
            elif result.state == "FAILED":
                runs_failed += 1
        except Exception:
            runs_failed += 1
            logger.exception(
                "personal context bootstrap runtime run failed run_id=%s",
                row.id,
            )

    return PersonalContextBootstrapRuntimeResult(
        runs_considered=len(rows),
        batches_completed=batches_completed,
        runs_completed=runs_completed,
        runs_requeued=runs_requeued,
        runs_failed=runs_failed,
    )


__all__ = [
    "PersonalContextBootstrapRuntimeResult",
    "run_personal_context_bootstrap_runtime_cycle",
]
