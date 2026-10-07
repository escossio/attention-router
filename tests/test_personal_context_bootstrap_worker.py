from __future__ import annotations

from attention_router.application.personal_context_bootstrap_runtime import (
    PersonalContextBootstrapRuntimeResult,
)
from attention_router.infrastructure import worker


def test_bootstrap_worker_is_default_off_and_interval_bounded(session, monkeypatch):
    calls = []
    adapter_builds = []

    class FakeAdapter:
        def __init__(self, **kwargs):
            adapter_builds.append(kwargs)

    def fake_cycle(session, *, adapter, run_limit, canary_tenant_id):
        calls.append(
            {
                "adapter": adapter,
                "run_limit": run_limit,
                "canary_tenant_id": canary_tenant_id,
            }
        )
        return PersonalContextBootstrapRuntimeResult(
            runs_considered=1,
            batches_completed=1,
            runs_requeued=1,
        )

    monkeypatch.setattr(worker, "WhatsAppHistoryAdapter", FakeAdapter)
    monkeypatch.setattr(
        worker,
        "run_personal_context_bootstrap_runtime_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_enabled",
        False,
    )

    result, last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is None
    assert last is None
    assert calls == []
    assert adapter_builds == []

    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_enabled",
        True,
    )
    result, last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is None and last is None and adapter_builds == []
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_interval_seconds",
        10,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_run_limit",
        3,
    )
    monkeypatch.setattr(
        worker.settings,
        "personal_context_bootstrap_canary_tenant_id",
        "tenant-canary",
    )
    monkeypatch.setattr(
        worker.settings,
        "whatsapp_history_url",
        "http://127.0.0.1:18103/internal/history/chats",
    )
    monkeypatch.setattr(
        worker.settings,
        "local_history_hmac_secret",
        "h" * 32,
    )
    monkeypatch.setattr(
        worker.settings,
        "whatsapp_history_timeout_seconds",
        2.0,
    )
    monkeypatch.setattr(
        worker.settings,
        "whatsapp_history_snapshot_limit",
        80,
    )

    result, last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=100.0,
        last_run_monotonic=None,
    )
    assert result is not None
    assert last == 100.0
    assert calls[-1]["run_limit"] == 3
    assert calls[-1]["canary_tenant_id"] == "tenant-canary"
    assert adapter_builds[-1]["snapshot_limit"] == 80
    assert adapter_builds[-1]["max_scan_messages"] == 1000
    assert adapter_builds[-1]["hmac_secret"] == "h" * 32

    result, same_last = worker.process_personal_context_bootstrap_runtime_if_due(
        session,
        now_monotonic=105.0,
        last_run_monotonic=last,
    )
    assert result is None
    assert same_last == last
    assert len(calls) == 1


def test_bootstrap_settings_are_default_off_and_bounded():
    from attention_router.config import Settings

    configured = Settings(_env_file=None)

    assert configured.personal_context_bootstrap_enabled is False
    assert configured.personal_context_bootstrap_canary_tenant_id is None
    assert configured.personal_context_bootstrap_interval_seconds == 10
    assert configured.personal_context_bootstrap_run_limit == 5
    assert configured.whatsapp_history_snapshot_limit == 100
