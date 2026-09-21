"""Governed automatic Gmail incremental polling runtime."""

from __future__ import annotations

from collections.abc import Callable, MutableSet
from dataclasses import dataclass
from datetime import datetime
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.gmail_history import (
    GmailProductHistoryBusy,
    GmailProductHistoryStale,
)
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationUnavailable,
    GmailProductRunner,
    GmailProductRunnerError,
)
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class GmailSchedulerCycleResult:
    selected: int = 0
    processed: int = 0
    initialized: int = 0
    busy: int = 0
    stale: int = 0
    quarantined: int = 0
    unavailable: int = 0
    failed: int = 0
    accepted: int = 0
    duplicates: int = 0
    cursor_advanced: int = 0
    last_installation_id: str | None = None


def _eligible_installations():
    return (
        ProviderAuthorizationRow.provider == "GOOGLE",
        ProviderAuthorizationRow.product == "GMAIL",
        ProviderAuthorizationRow.status == "ACTIVE",
        ProviderAuthorizationRow.revoked_at.is_(None),
    )

def discover_active_installation_ids(
    session: Session,
    *,
    limit: int,
    after_id: str | None = None,
) -> tuple[str, ...]:
    """Return one fair bounded page, wrapping after the current cursor."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("GMAIL_SCHEDULER_BATCH_LIMIT_OUT_OF_RANGE")

    base = select(ProviderAuthorizationRow.id).where(*_eligible_installations())
    if after_id is None:
        return tuple(
            session.scalars(
                base.order_by(ProviderAuthorizationRow.id).limit(limit)
            )
        )

    forward = tuple(
        session.scalars(
            base.where(ProviderAuthorizationRow.id > after_id)
            .order_by(ProviderAuthorizationRow.id)
            .limit(limit)
        )
    )
    remaining = limit - len(forward)
    if remaining == 0:
        return forward

    wrapped = tuple(
        session.scalars(
            base.where(ProviderAuthorizationRow.id <= after_id)
            .order_by(ProviderAuthorizationRow.id)
            .limit(remaining)
        )
    )
    return forward + wrapped


def run_scheduler_cycle(
    session_factory: Callable[[], Session],
    runner: GmailProductRunner,
    *,
    limit: int,
    max_results: int,
    max_pages: int,
    after_id: str | None = None,
    stale_installations: MutableSet[str] | None = None,
    now: datetime | None = None,
) -> GmailSchedulerCycleResult:
    """Discover first, then give every installation its own transaction."""
    stale_installations = (
        stale_installations if stale_installations is not None else set()
    )
    with session_factory() as discovery:
        installation_ids = discover_active_installation_ids(
            discovery,
            limit=limit,
            after_id=after_id,
        )

    processed = initialized = busy = stale = quarantined = 0
    unavailable = failed = accepted = duplicates = advanced = 0

    for installation_id in installation_ids:
        if installation_id in stale_installations:
            quarantined += 1
            continue

        with session_factory() as session:
            try:
                result = runner.run_incremental(
                    session,
                    installation_id=installation_id,
                    max_results=max_results,
                    max_pages=max_pages,
                    now=now,
                )
                session.commit()
            except GmailProductHistoryBusy:
                session.rollback()
                busy += 1
                continue
            except GmailProductHistoryStale:
                session.rollback()
                stale_installations.add(installation_id)
                stale += 1
                continue

            except GmailProductAuthorizationUnavailable:
                session.rollback()
                unavailable += 1
                continue
            except GmailProductRunnerError as exc:
                session.rollback()
                failed += 1
                logger.warning(
                    "gmail scheduler installation failed installation_id=%s code=%s",
                    installation_id,
                    exc.code,
                )
                continue
            except Exception as exc:
                session.rollback()
                failed += 1
                logger.error(
                    "gmail scheduler installation failed installation_id=%s error_type=%s",
                    installation_id,
                    type(exc).__name__,
                )
                continue

        processed += 1
        initialized += int(result.initialized)
        accepted += result.accepted
        duplicates += result.duplicates
        advanced += int(result.cursor_advanced)

    return GmailSchedulerCycleResult(
        selected=len(installation_ids),
        processed=processed,
        initialized=initialized,
        busy=busy,
        stale=stale,
        quarantined=quarantined,
        unavailable=unavailable,
        failed=failed,
        accepted=accepted,
        duplicates=duplicates,
        cursor_advanced=advanced,
        last_installation_id=(
            installation_ids[-1] if installation_ids else after_id
        ),
    )


def run_forever() -> None:
    if not settings.gmail_product_scheduler_enabled:
        logger.info("gmail product scheduler disabled")
        return

    runner = GmailProductRunner(settings=settings)
    after_id: str | None = None
    stale_installations: set[str] = set()
    logger.info("gmail product scheduler started")

    while True:
        try:
            result = run_scheduler_cycle(
                SessionLocal,
                runner,
                limit=settings.gmail_product_scheduler_batch_size,
                max_results=settings.gmail_product_runner_max_results,
                max_pages=settings.gmail_product_scheduler_max_pages,
                after_id=after_id,
                stale_installations=stale_installations,
            )
            after_id = result.last_installation_id
            if (
                result.processed
                or result.busy
                or result.stale
                or result.quarantined
                or result.unavailable
                or result.failed
            ):
                logger.info(
                    "gmail scheduler cycle selected=%s processed=%s "
                    "initialized=%s accepted=%s duplicates=%s advanced=%s "
                    "busy=%s stale=%s quarantined=%s unavailable=%s failed=%s",
                    result.selected,
                    result.processed,
                    result.initialized,
                    result.accepted,
                    result.duplicates,
                    result.cursor_advanced,
                    result.busy,
                    result.stale,

                    result.quarantined,
                    result.unavailable,
                    result.failed,
                )
        except Exception as exc:
            logger.error(
                "gmail scheduler cycle failed error_type=%s",
                type(exc).__name__,
            )
        time.sleep(settings.gmail_product_scheduler_poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
