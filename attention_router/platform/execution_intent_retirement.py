"""Canonical persistence seam for retiring inert execution intents."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import ExecutionIntentRow
from attention_router.platform.production_authority import ProductionAuthorityDenied


def retire_execution_intent(
    session: Session,
    *,
    execution_intent_id: str,
    expected_fingerprint: str | None = None,
    now: datetime | None = None,
) -> ExecutionIntentRow:
    """Atomically retire a PREPARED/FROZEN intent; replay is idempotent."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    row = session.execute(
        select(ExecutionIntentRow)
        .where(ExecutionIntentRow.id == execution_intent_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_FOUND")
    if expected_fingerprint is not None and row.scope_fingerprint != expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_MISMATCH")
    if row.state == "RETIRED":
        return row
    if row.state not in {"PREPARED", "FROZEN"}:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_NOT_RETIRABLE")
    if row.state == "FROZEN" and not expected_fingerprint:
        raise ProductionAuthorityDenied("EXECUTION_INTENT_FINGERPRINT_REQUIRED")
    row.state = "RETIRED"
    row.retired_at = timestamp
    session.flush()
    return row


__all__ = ["retire_execution_intent"]

