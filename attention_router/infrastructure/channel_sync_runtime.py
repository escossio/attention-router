"""Provider-neutral live channel synchronization runtime."""

from __future__ import annotations

from collections.abc import Callable, MutableSet
from dataclasses import dataclass
from datetime import datetime
import logging

from sqlalchemy.orm import Session

from attention_router.application.channel_sync import (
    ChannelSyncAdapter,
    ChannelSyncBusy,
    ChannelSyncError,
    ChannelSyncProviderFailure,
    ChannelSyncStale,
    ChannelSyncUnavailable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelSyncCycleResult:
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


def run_channel_sync_cycle(
    session_factory: Callable[[], Session],
    adapter: ChannelSyncAdapter,
    *,
    limit: int,
    after_id: str | None = None,
    stale_installations: MutableSet[str] | None = None,
    now: datetime | None = None,
) -> ChannelSyncCycleResult:
    """Run one bounded live-continuity cycle with per-installation isolation."""
    if not adapter.capabilities.live_continuity:
        raise ValueError("CHANNEL_SYNC_LIVE_CONTINUITY_UNSUPPORTED")

    quarantined_ids = (
        stale_installations if stale_installations is not None else set()
    )
    with session_factory() as discovery:
        installation_ids = adapter.discover_live_installation_ids(
            discovery,
            limit=limit,
            after_id=after_id,
        )

    processed = initialized = busy = stale = quarantined = 0
    unavailable = failed = accepted = duplicates = advanced = 0

    for installation_id in installation_ids:
        if installation_id in quarantined_ids:
            quarantined += 1
            continue

        with session_factory() as session:
            try:
                result = adapter.run_live(
                    session,
                    installation_id=installation_id,
                    now=now,
                )
                session.commit()
            except ChannelSyncBusy:
                session.rollback()
                busy += 1
                continue
            except ChannelSyncStale:
                session.rollback()
                quarantined_ids.add(installation_id)
                stale += 1
                continue
            except ChannelSyncUnavailable:
                session.rollback()
                unavailable += 1
                continue
            except ChannelSyncProviderFailure as exc:
                session.rollback()
                failed += 1
                logger.warning(
                    "channel sync installation failed adapter=%s "
                    "installation_id=%s code=%s",
                    adapter.key,
                    installation_id,
                    exc.code,
                )
                continue
            except ChannelSyncError as exc:
                session.rollback()
                failed += 1
                logger.warning(
                    "channel sync installation failed adapter=%s "
                    "installation_id=%s code=%s",
                    adapter.key,
                    installation_id,
                    exc.code,
                )
                continue
            except Exception as exc:
                session.rollback()
                failed += 1
                logger.error(
                    "channel sync installation failed adapter=%s "
                    "installation_id=%s error_type=%s",
                    adapter.key,
                    installation_id,
                    type(exc).__name__,
                )
                continue

        processed += 1
        initialized += int(result.initialized)
        accepted += result.accepted
        duplicates += result.duplicates
        advanced += int(result.cursor_advanced)

    return ChannelSyncCycleResult(
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
