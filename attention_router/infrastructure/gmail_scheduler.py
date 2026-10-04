"""Governed automatic Gmail incremental polling runtime."""

from __future__ import annotations

from collections.abc import Callable, MutableSet
from datetime import datetime
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.channel_sync import (
    ChannelSyncBusy,
    ChannelSyncCapabilities,
    ChannelSyncLiveResult,
    ChannelSyncProviderFailure,
    ChannelSyncStale,
    ChannelSyncUnavailable,
)
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
from attention_router.infrastructure.channel_sync_runtime import (
    ChannelSyncCycleResult,
    run_channel_sync_cycle,
)
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.provider_authorization_models import (
    ProviderAuthorizationRow,
)

logger = logging.getLogger(__name__)

GmailSchedulerCycleResult = ChannelSyncCycleResult


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


class GmailChannelSyncAdapter:
    """Translate the certified Gmail live path into the neutral runtime contract."""

    key = "google.gmail"
    capabilities = ChannelSyncCapabilities(
        live_continuity=True,
        historical_acceleration=False,
    )

    def __init__(
        self,
        runner: GmailProductRunner,
        *,
        max_results: int,
        max_pages: int,
    ):
        self.runner = runner
        self.max_results = max_results
        self.max_pages = max_pages

    def discover_live_installation_ids(
        self,
        session: Session,
        *,
        limit: int,
        after_id: str | None = None,
    ) -> tuple[str, ...]:
        return discover_active_installation_ids(
            session,
            limit=limit,
            after_id=after_id,
        )

    def run_live(
        self,
        session: Session,
        *,
        installation_id: str,
        now: datetime | None = None,
    ) -> ChannelSyncLiveResult:
        try:
            result = self.runner.run_incremental(
                session,
                installation_id=installation_id,
                max_results=self.max_results,
                max_pages=self.max_pages,
                now=now,
            )
        except GmailProductHistoryBusy:
            raise ChannelSyncBusy() from None
        except GmailProductHistoryStale:
            raise ChannelSyncStale() from None
        except GmailProductAuthorizationUnavailable:
            raise ChannelSyncUnavailable() from None
        except GmailProductRunnerError as exc:
            raise ChannelSyncProviderFailure(exc.code) from None

        return ChannelSyncLiveResult(
            initialized=result.initialized,
            accepted=result.accepted,
            duplicates=result.duplicates,
            cursor_advanced=result.cursor_advanced,
        )


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
    """Run Gmail through the provider-neutral live-continuity cycle."""
    adapter = GmailChannelSyncAdapter(
        runner,
        max_results=max_results,
        max_pages=max_pages,
    )
    return run_channel_sync_cycle(
        session_factory,
        adapter,
        limit=limit,
        after_id=after_id,
        stale_installations=stale_installations,
        now=now,
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
