"""Default-off bounded runtime for queued Personal Context bootstrap runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_bootstrap import (
    process_next_bootstrap_batch,
)
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.integrations.whatsapp_history import (
    LocalWhatsAppHistoryAdapter,
)


@dataclass(frozen=True, slots=True)
class PersonalContextBootstrapRuntimeResult:
    runs_considered: int = 0
    runs_succeeded: int = 0
    runs_failed: int = 0
    runs_completed: int = 0
    batches_processed: int = 0
    messages_discovered: int = 0
    messages_archived: int = 0


def run_personal_context_bootstrap_cycle(
    session: Session,
    *,
    adapter: LocalWhatsAppHistoryAdapter,
    source_account: str,
    run_limit: int = 2,
    canary_tenant_id: str | None = None,
    now: datetime | None = None,
) -> PersonalContextBootstrapRuntimeResult:
    if type(run_limit) is not int or not 1 <= run_limit <= 20:
        raise ValueError("BOOTSTRAP_RUNTIME_RUN_LIMIT_OUT_OF_RANGE")
    if not isinstance(source_account, str) or not source_account:
        raise ValueError("BOOTSTRAP_RUNTIME_SOURCE_ACCOUNT_REQUIRED")

    query = select(PersonalContextBootstrapRunRow).where(
        PersonalContextBootstrapRunRow.state == "QUEUED",
        PersonalContextBootstrapRunRow.source_kind == "WHATSAPP_TEXT",
        PersonalContextBootstrapRunRow.source_account == source_account,
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
            ).limit(run_limit)
        ).all()
    )

    succeeded = 0
    failed = 0
    completed = 0
    batches = 0
    messages_discovered = 0
    messages_archived = 0

    for row in rows:
        try:
            with session.begin_nested():
                result = process_next_bootstrap_batch(
                    session,
                    row.id,
                    adapter=adapter,
                    now=now,
                )
            batches += 1 if result.batch_id is not None else 0
            messages_discovered += int(
                result.metrics.get("total_messages_discovered", 0)
            )
            messages_archived += int(
                result.metrics.get("total_messages_archived", 0)
            )
            if result.state == "FAILED":
                failed += 1
            else:
                succeeded += 1
                if result.state == "COMPLETED":
                    completed += 1
        except Exception:
            failed += 1

    return PersonalContextBootstrapRuntimeResult(
        runs_considered=len(rows),
        runs_succeeded=succeeded,
        runs_failed=failed,
        runs_completed=completed,
        batches_processed=batches,
        messages_discovered=messages_discovered,
        messages_archived=messages_archived,
    )


__all__ = [
    "PersonalContextBootstrapRuntimeResult",
    "run_personal_context_bootstrap_cycle",
]
