"""Provider-neutral live channel synchronization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ChannelSyncCapabilities:
    """Declare which independent channel-context tracks an adapter supports."""

    live_continuity: bool = True
    historical_acceleration: bool = False


@dataclass(frozen=True, slots=True)
class ChannelSyncLiveResult:
    """Provider-neutral aggregate from one successful live installation sync."""

    initialized: bool = False
    accepted: int = 0
    duplicates: int = 0
    cursor_advanced: bool = False


class ChannelSyncError(RuntimeError):
    code = "CHANNEL_SYNC_ERROR"

    def __init__(self, code: str | None = None):
        resolved = code or self.code
        self.code = resolved
        super().__init__(resolved)


class ChannelSyncBusy(ChannelSyncError):
    code = "CHANNEL_SYNC_BUSY"


class ChannelSyncStale(ChannelSyncError):
    code = "CHANNEL_SYNC_STALE"


class ChannelSyncUnavailable(ChannelSyncError):
    code = "CHANNEL_SYNC_UNAVAILABLE"


class ChannelSyncProviderFailure(ChannelSyncError):
    code = "CHANNEL_SYNC_PROVIDER_FAILURE"


class ChannelSyncAdapter(Protocol):
    """Minimal live-continuity boundary implemented by concrete providers."""

    key: str
    capabilities: ChannelSyncCapabilities

    def discover_live_installation_ids(
        self,
        session: Session,
        *,
        limit: int,
        after_id: str | None = None,
    ) -> tuple[str, ...]: ...

    def run_live(
        self,
        session: Session,
        *,
        installation_id: str,
        now: datetime | None = None,
    ) -> ChannelSyncLiveResult: ...
