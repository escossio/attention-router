"""Dedicated outbound-only live Channel Sync process; static adapters only."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import logging
import signal
from threading import Event
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from attention_router.application.channel_sync import ChannelSyncAdapter
    from attention_router.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredAdapter:
    adapter: ChannelSyncAdapter
    enabled: bool
    batch_size: int
    interval_seconds: int


@dataclass
class AdapterSchedule:
    after_id: str | None = None
    stale_installations: set[str] = field(default_factory=set)
    next_run: float = 0.0


def build_channel_sync_registry(settings: Settings) -> dict[str, RegisteredAdapter]:
    """Build the closed V1 registry without connecting to DB or providers."""
    from sqlalchemy.engine import make_url

    from attention_router.application.gmail_product_runner import GmailProductRunner
    from attention_router.config import Settings
    from attention_router.infrastructure.gmail_scheduler import GmailChannelSyncAdapter

    # Revalidate even Settings instances changed by an embedding caller.
    configured = Settings(_env_file=None, **settings.model_dump())
    if configured.gmail_attachment_ingestion_enabled:
        raise ValueError("CHANNEL_SYNC_ATTACHMENTS_UNSUPPORTED")
    database = make_url(configured.database_url)
    if configured.gmail_product_scheduler_enabled:
        if database.drivername != "postgresql+psycopg" or not database.database:
            raise ValueError("CHANNEL_SYNC_POSTGRES_REQUIRED")
        ingress = urlsplit(configured.gmail_product_runner_ingress_url)
        if (
            ingress.scheme not in {"http", "https"}
            or not ingress.hostname
            or ingress.username is not None
            or ingress.password is not None
            or ingress.query
            or ingress.fragment
            or ingress.path != "/api/v1/ingress/integrations/events"
        ):
            raise ValueError("CHANNEL_SYNC_INGRESS_CONFIG_INVALID")
        _ = ingress.port  # Reject malformed port syntax without resolving the host.

    adapter = GmailChannelSyncAdapter(
        GmailProductRunner(settings=configured),
        max_results=configured.gmail_product_runner_max_results,
        max_pages=configured.gmail_product_scheduler_max_pages,
    )
    return {
        "google.gmail": RegisteredAdapter(
            adapter=adapter,
            enabled=configured.gmail_product_scheduler_enabled,
            batch_size=configured.gmail_product_scheduler_batch_size,
            interval_seconds=configured.gmail_product_scheduler_poll_interval_seconds,
        ),
    }


def run_service(
    registry: Mapping[str, RegisteredAdapter],
    session_factory: Callable[[], Session],
    stop: Event,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Serial bounded cycles; each adapter owns its rotation and quarantine."""
    from attention_router.infrastructure.channel_sync_runtime import run_channel_sync_cycle

    enabled = {key: entry for key, entry in registry.items() if entry.enabled}
    if not enabled:
        logger.info("channel sync disabled: no enabled adapters")
        return
    schedules = {key: AdapterSchedule() for key in enabled}
    while not stop.is_set():
        for key, entry in enabled.items():
            state = schedules[key]
            if stop.is_set():
                break
            if monotonic() < state.next_run:
                continue
            try:
                result = run_channel_sync_cycle(
                    session_factory,
                    entry.adapter,
                    limit=entry.batch_size,
                    after_id=state.after_id,
                    stale_installations=state.stale_installations,
                )
                state.after_id = result.last_installation_id
                logger.info(
                    "channel sync cycle adapter=%s selected=%s processed=%s "
                    "initialized=%s accepted=%s duplicates=%s cursor_advanced=%s "
                    "busy=%s stale=%s quarantined=%s unavailable=%s failed=%s",
                    key, result.selected, result.processed, result.initialized,
                    result.accepted, result.duplicates, result.cursor_advanced,
                    result.busy, result.stale, result.quarantined,
                    result.unavailable, result.failed,
                )
            except Exception:
                # Discovery/session failures can contain credentials or SQL values.
                logger.error("channel sync cycle adapter=%s code=CHANNEL_SYNC_CYCLE_FAILED", key)
            state.next_run = monotonic() + entry.interval_seconds
        stop.wait(max(0.0, min(state.next_run for state in schedules.values()) - monotonic()))
    logger.info("channel sync stopped")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline structural config check")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        # Settings has an import-time singleton. Keep its validation and all
        # dependent imports inside this boundary: Pydantic errors contain inputs.
        from attention_router.config import settings
        from attention_router.infrastructure.db import SessionLocal

        registry = build_channel_sync_registry(settings)
    except Exception:
        logger.error("CHANNEL_SYNC_CONFIG_INVALID")
        return 2
    if args.check:
        for key, entry in registry.items():
            logger.info("channel sync config adapter=%s enabled=%s", key, entry.enabled)
        return 0

    stop = Event()
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        run_service(registry, SessionLocal, stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
