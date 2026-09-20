from attention_router.infrastructure import worker
from attention_router.integrations.dispatch import IntegrationDispatchResult


def test_integration_dispatch_worker_gate_is_off_by_default(session, monkeypatch):
    monkeypatch.setattr(
        worker.settings,
        "integration_dispatch_enabled",
        False,
    )

    result = worker.process_integration_dispatch_if_enabled(session)

    assert result is None


def test_integration_dispatch_worker_gate_forwards_bounded_batch(
    session,
    monkeypatch,
):
    calls = []

    def fake_dispatch(_session, **kwargs):
        calls.append(kwargs)
        return IntegrationDispatchResult(
            selected=2,
            processed=1,
            blocked=1,
        )

    monkeypatch.setattr(
        worker.settings,
        "integration_dispatch_enabled",
        True,
    )
    monkeypatch.setattr(
        worker.settings,
        "integration_dispatch_batch_size",
        17,
    )
    monkeypatch.setattr(
        worker,
        "process_integration_inbox",
        fake_dispatch,
    )

    result = worker.process_integration_dispatch_if_enabled(session)

    assert result is not None
    assert result.selected == 2
    assert result.processed == 1
    assert result.blocked == 1
    assert calls == [{"limit": 17}]
