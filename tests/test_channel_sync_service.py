from dataclasses import replace
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Event
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from attention_router.application.channel_sync import ChannelSyncLiveResult, ChannelSyncStale
from attention_router.application.gmail_history import GmailHistoryResult
from attention_router.application.gmail_product_runner import (
    GmailProductRunner,
    GoogleRefreshAccessTokenClient,
)
from attention_router.config import Settings
from attention_router.infrastructure import channel_sync_runtime, channel_sync_service as service
from attention_router.infrastructure import gmail_scheduler
from attention_router.integrations.gmail_api_reader import GmailApiReader
from attention_router.integrations.gmail_connector import IntegrationIngressClient


def configured(*, enabled=False, **overrides):
    values = dict(
        _env_file=None,
        app_env="test",
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="synthetic-test-hmac-value-32-characters",
        database_url="postgresql+psycopg://synthetic:synthetic@database.invalid/synthetic",
        client_session_enabled=enabled,
        gmail_connect_enabled=enabled,
        gmail_product_runner_enabled=enabled,
        gmail_product_scheduler_enabled=enabled,
        google_workspace_oauth_client_id="synthetic-client",
        google_workspace_oauth_client_secret="synthetic-secret",
        provider_authorization_key_b64url="A" * 43,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def deny_io(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("configuration must not perform I/O")

    for cls, name in (
        (socket.socket, "connect"), (Engine, "connect"),
        (Session, "execute"), (Session, "flush"), (Session, "commit"),
        (GmailProductRunner, "run_incremental"),
        (GoogleRefreshAccessTokenClient, "refresh_access_token"),
        (IntegrationIngressClient, "send"),
        (GmailApiReader, "__init__"),
    ):
        monkeypatch.setattr(cls, name, denied)


def test_registry_is_exact_static_gmail_and_constructs_without_io(deny_io):
    registry = service.build_channel_sync_registry(configured(enabled=True))
    assert tuple(registry) == ("google.gmail",)
    entry = registry["google.gmail"]
    assert isinstance(entry.adapter, gmail_scheduler.GmailChannelSyncAdapter)
    assert isinstance(entry.adapter.runner, GmailProductRunner)
    assert entry.enabled
    assert (entry.batch_size, entry.interval_seconds) == (20, 30)
    assert (entry.adapter.max_results, entry.adapter.max_pages) == (5, 10)


def test_registry_accepts_attachment_mode_with_artifact_store_without_io(deny_io):
    registry = service.build_channel_sync_registry(
        configured(
            enabled=True,
            gmail_attachment_ingestion_enabled=True,
            artifact_store_enabled=True,
            artifact_store_root="/var/lib/attention-router/artifacts",
        )
    )
    entry = registry["google.gmail"]
    assert entry.enabled
    assert entry.adapter.runner.settings.gmail_attachment_ingestion_enabled is True
    assert entry.adapter.runner.settings.artifact_store_enabled is True
    assert entry.adapter.runner.settings.artifact_store_root == (
        "/var/lib/attention-router/artifacts"
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_check_never_calls_provider_database_or_ingress(enabled, deny_io, monkeypatch, caplog):
    import attention_router.config

    monkeypatch.setattr(attention_router.config, "settings", configured(enabled=enabled))
    with caplog.at_level(logging.INFO):
        assert service.main(["--check"]) == 0
    assert "google.gmail" in caplog.text
    assert "synthetic-secret" not in caplog.text


def test_disabled_service_exits_without_discovery_or_wait(deny_io):
    registry = service.build_channel_sync_registry(configured())
    stop = Mock()
    stop.is_set.return_value = False
    service.run_service(registry, Mock(side_effect=AssertionError("DB called")), stop)
    stop.wait.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"gmail_product_runner_enabled": False},
    {"google_workspace_oauth_client_secret": None},
    {"gmail_product_scheduler_batch_size": 0},
    {"gmail_product_scheduler_poll_interval_seconds": 0},
    {"gmail_product_scheduler_max_pages": 11},
    {"gmail_product_runner_max_results": 101},
    {"database_url": "sqlite+pysqlite:///:memory:"},
    {"database_url": "not-a-url"},
    {"gmail_product_runner_ingress_url": "http:///missing-host"},
    {"gmail_product_runner_ingress_url": "http://ingress.invalid:bad/api/v1/ingress/integrations/events"},
    {"gmail_product_runner_ingress_url": "http://ingress.invalid/health/live"},
    {"gmail_product_runner_ingress_url": "http://user:secret@ingress.invalid/api/v1/ingress/integrations/events"},
])
def test_invalid_configuration_fails_closed(changes, monkeypatch, deny_io, caplog):
    import attention_router.config

    # model_copy intentionally bypasses validation; the builder must revalidate.
    monkeypatch.setattr(attention_router.config, "settings", configured(enabled=True).model_copy(
        update=changes,
    ))
    assert service.main(["--check"]) == 2
    assert "CHANNEL_SYNC_CONFIG_INVALID" in caplog.text
    assert "Traceback" not in caplog.text
    assert "synthetic-secret" not in caplog.text


def test_cli_import_time_validation_is_sanitized(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "APP_ENV": "private-config-sentinel",
        "ADMIN_AUTH_ENABLED": "false",
        "INTERNAL_INGRESS_HMAC_SECRET": "synthetic-test-hmac-value-32-characters",
        "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET": "private-oauth-sentinel",
    }
    result = subprocess.run(
        [sys.executable, "-m", service.__name__, "--check"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "CHANNEL_SYNC_CONFIG_INVALID" in output
    assert "private-config-sentinel" not in output
    assert "private-oauth-sentinel" not in output
    assert "Traceback" not in output


class FakeSession:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class Clock(Event):
    def __init__(self, *, ticks=2):
        super().__init__()
        self.now = 0.0
        self.waits = []
        self.ticks = ticks

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.now += timeout
        if len(self.waits) >= self.ticks:
            self.set()
        return self.is_set()


def test_service_uses_runtime_and_existing_gmail_runner(monkeypatch, caplog):
    registry = service.build_channel_sync_registry(configured(enabled=True))
    adapter = registry["google.gmail"].adapter
    monkeypatch.setattr(adapter, "discover_live_installation_ids", Mock(return_value=("synthetic",)))
    runner = Mock(return_value=GmailHistoryResult("synthetic", accepted=2, duplicates=1))
    monkeypatch.setattr(adapter.runner, "run_incremental", runner)
    sessions = []

    def factory():
        session = FakeSession()
        sessions.append(session)
        return session

    clock = Clock(ticks=1)
    with caplog.at_level(logging.INFO):
        service.run_service(registry, factory, clock, monotonic=lambda: clock.now)
    assert len(sessions) == 2
    assert [s.commits for s in sessions] == [0, 1]
    runner.assert_called_once_with(
        sessions[1], installation_id="synthetic", max_results=5, max_pages=10, now=None,
    )
    assert "adapter=google.gmail selected=1 processed=1" in caplog.text
    assert "accepted=2 duplicates=1" in caplog.text


def test_adapter_state_and_intervals_are_independent(monkeypatch):
    entry = service.build_channel_sync_registry(configured(enabled=True))["google.gmail"]
    first = Mock(key="synthetic.first")
    second = Mock(key="synthetic.second")
    first.capabilities.live_continuity = second.capabilities.live_continuity = True
    first.discover_live_installation_ids.return_value = ("same-id",)
    second.discover_live_installation_ids.return_value = ("same-id", "second-last")
    first.run_live.side_effect = ChannelSyncStale()
    second.run_live.return_value = ChannelSyncLiveResult()
    registry = {
        first.key: replace(entry, adapter=first, interval_seconds=10),
        second.key: replace(entry, adapter=second, interval_seconds=20),
    }
    clock = Clock(ticks=3)
    service.run_service(registry, FakeSession, clock, monotonic=lambda: clock.now)
    assert clock.waits == [10, 10, 10]
    assert first.run_live.call_count == 1  # quarantined for subsequent cycles
    assert second.run_live.call_count == 4  # same ID is not quarantined here
    assert first.discover_live_installation_ids.call_count == 3
    assert second.discover_live_installation_ids.call_count == 2
    for adapter, cursor in ((first, "same-id"), (second, "second-last")):
        calls = adapter.discover_live_installation_ids.call_args_list
        assert calls[0].kwargs["after_id"] is None
        assert calls[1].kwargs["after_id"] == cursor


def test_untrusted_discovery_failure_is_sanitized_and_next_adapter_runs(monkeypatch, caplog):
    entry = service.build_channel_sync_registry(configured(enabled=True))["google.gmail"]
    good = replace(entry, adapter=Mock())
    registry = {"google.gmail": entry, "synthetic.other": good}
    cycle = Mock(side_effect=[
        RuntimeError("body refresh-token access-token integration-bearer private-sentinel"),
        channel_sync_runtime.ChannelSyncCycleResult(processed=1),
    ])
    monkeypatch.setattr(channel_sync_runtime, "run_channel_sync_cycle", cycle)
    with caplog.at_level(logging.INFO):
        service.run_service(registry, FakeSession, Clock(ticks=1))
    assert cycle.call_count == 2
    assert "CHANNEL_SYNC_CYCLE_FAILED" in caplog.text
    assert "adapter=synthetic.other" in caplog.text
    assert "private-sentinel" not in caplog.text
    assert "Traceback" not in caplog.text


def test_main_handles_stop_signal_and_restores_handlers(monkeypatch):
    import attention_router.config

    monkeypatch.setattr(attention_router.config, "settings", configured(enabled=True))
    installed = {}
    previous = {}

    def register(signum, handler):
        old = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return old

    def run(registry, factory, stop):
        assert not stop.is_set()
        previous.update(installed)
        installed[signal.SIGTERM](signal.SIGTERM, None)
        assert stop.is_set()

    monkeypatch.setattr(signal, "signal", register)
    monkeypatch.setattr(service, "run_service", run)
    assert service.main([]) == 0
    assert set(previous) == {signal.SIGTERM, signal.SIGINT}
    assert all(handler == signal.SIG_DFL for handler in installed.values())


def test_legacy_disabled_entrypoint_still_exits_without_runner(monkeypatch):
    monkeypatch.setattr(gmail_scheduler, "settings", configured())
    runner = Mock(side_effect=AssertionError("disabled legacy entrypoint did work"))
    monkeypatch.setattr(gmail_scheduler, "GmailProductRunner", runner)
    gmail_scheduler.run_forever()
    runner.assert_not_called()


def test_check_in_fresh_process_does_not_connect(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "APP_ENV": "test",
        "ADMIN_AUTH_ENABLED": "false",
        "INTERNAL_INGRESS_HMAC_SECRET": "synthetic-test-hmac-value-32-characters",
        "DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@database.invalid/synthetic",
        "CLIENT_SESSION_ENABLED": "true",
        "GMAIL_CONNECT_ENABLED": "true",
        "GMAIL_PRODUCT_RUNNER_ENABLED": "true",
        "GMAIL_PRODUCT_SCHEDULER_ENABLED": "true",
        "GOOGLE_WORKSPACE_OAUTH_CLIENT_ID": "synthetic-client",
        "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "PROVIDER_AUTHORIZATION_KEY_B64URL": "A" * 43,
    }
    script = """
import socket
import sys
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

def denied(*args, **kwargs):
    raise AssertionError("forbidden I/O")

socket.socket.connect = denied
Engine.connect = denied
Session.commit = denied
Session.flush = denied
sys.argv = ["channel-sync", "--check"]
import runpy
runpy.run_module("attention_router.infrastructure.channel_sync_service", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "adapter=google.gmail enabled=True" in result.stderr
