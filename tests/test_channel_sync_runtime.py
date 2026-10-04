from __future__ import annotations

from datetime import UTC, datetime

import pytest

from attention_router.application.channel_sync import (
    ChannelSyncBusy,
    ChannelSyncCapabilities,
    ChannelSyncLiveResult,
    ChannelSyncProviderFailure,
    ChannelSyncStale,
    ChannelSyncUnavailable,
)
from attention_router.infrastructure.channel_sync_runtime import (
    run_channel_sync_cycle,
)

NOW = datetime(2026, 10, 4, 22, 0, tzinfo=UTC)


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _SessionFactory:
    def __init__(self):
        self.sessions: list[_Session] = []

    def __call__(self):
        session = _Session()
        self.sessions.append(session)
        return session


class _Adapter:
    key = "synthetic.channel"
    capabilities = ChannelSyncCapabilities(
        live_continuity=True,
        historical_acceleration=False,
    )

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def discover_live_installation_ids(
        self,
        session,
        *,
        limit,
        after_id=None,
    ):
        assert limit == 6
        assert after_id == "cursor-before"
        return ("a", "b", "c", "d", "e", "f")

    def run_live(self, session, *, installation_id, now=None):
        self.calls.append((installation_id, now))
        outcome = self.outcomes[installation_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_cycle_isolates_installations_and_classifies_neutral_failures(caplog):
    factory = _SessionFactory()
    adapter = _Adapter(
        {
            "a": ChannelSyncLiveResult(
                initialized=True,
                accepted=2,
                duplicates=1,
                cursor_advanced=True,
            ),
            "b": ChannelSyncBusy(),
            "c": ChannelSyncStale(),
            "d": ChannelSyncUnavailable(),
            "e": ChannelSyncProviderFailure("SYNTHETIC_PROVIDER_FAILURE"),
            "f": RuntimeError("private-provider-error-must-not-be-logged"),
        }
    )
    stale_installations = set()

    result = run_channel_sync_cycle(
        factory,
        adapter,
        limit=6,
        after_id="cursor-before",
        stale_installations=stale_installations,
        now=NOW,
    )

    assert result.selected == 6
    assert result.processed == 1
    assert result.initialized == 1
    assert result.busy == 1
    assert result.stale == 1
    assert result.unavailable == 1
    assert result.failed == 2
    assert result.accepted == 2
    assert result.duplicates == 1
    assert result.cursor_advanced == 1
    assert result.last_installation_id == "f"
    assert stale_installations == {"c"}
    assert adapter.calls == [
        ("a", NOW),
        ("b", NOW),
        ("c", NOW),
        ("d", NOW),
        ("e", NOW),
        ("f", NOW),
    ]

    assert len(factory.sessions) == 7
    assert factory.sessions[0].commits == 0
    assert factory.sessions[0].rollbacks == 0
    assert factory.sessions[1].commits == 1
    assert factory.sessions[1].rollbacks == 0
    for session in factory.sessions[2:]:
        assert session.commits == 0
        assert session.rollbacks == 1

    assert "SYNTHETIC_PROVIDER_FAILURE" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private-provider-error-must-not-be-logged" not in caplog.text


def test_cycle_skips_quarantined_installation_without_opening_work_session():
    factory = _SessionFactory()
    adapter = _Adapter(
        {
            "a": AssertionError("quarantined installation was called"),
            "b": ChannelSyncLiveResult(),
            "c": ChannelSyncLiveResult(),
            "d": ChannelSyncLiveResult(),
            "e": ChannelSyncLiveResult(),
            "f": ChannelSyncLiveResult(),
        }
    )

    result = run_channel_sync_cycle(
        factory,
        adapter,
        limit=6,
        after_id="cursor-before",
        stale_installations={"a"},
        now=NOW,
    )

    assert result.selected == 6
    assert result.quarantined == 1
    assert result.processed == 5
    assert [call[0] for call in adapter.calls] == ["b", "c", "d", "e", "f"]
    assert len(factory.sessions) == 6


def test_cycle_refuses_adapter_without_live_continuity():
    factory = _SessionFactory()
    adapter = _Adapter({})
    adapter.capabilities = ChannelSyncCapabilities(
        live_continuity=False,
        historical_acceleration=True,
    )

    with pytest.raises(
        ValueError,
        match="CHANNEL_SYNC_LIVE_CONTINUITY_UNSUPPORTED",
    ):
        run_channel_sync_cycle(
            factory,
            adapter,
            limit=6,
            after_id="cursor-before",
        )

    assert factory.sessions == []
