"""Synthetic, same-host maintenance admission tests. No production inputs."""
import asyncio
import json
import multiprocessing
import os
import threading
from uuid import uuid4

import pytest
from starlette.exceptions import HTTPException

from attention_router.infrastructure.maintenance_gate import (
    ENV,
    GateError,
    MaintenanceGate,
    observe_runtime,
    runtime_admission,
)
from attention_router.web.maintenance_gate import MaintenanceMiddleware, guarded_ingress_call


def make_gate(tmp_path, monkeypatch, participants=("worker",)):
    gate = MaintenanceGate.initialize(tmp_path / "gate", list(participants))
    monkeypatch.setenv(ENV, str(gate.directory))
    return gate


def _child_cycle(directory, started, finish):
    gate = MaintenanceGate(directory)
    with gate.admission("worker") as admitted:
        assert admitted
        started.set()
        assert finish.wait(10)


def _child_abandon(directory):
    gate = MaintenanceGate(directory)
    with gate.admission("worker") as admitted:
        assert admitted
        os._exit(23)


def test_unconfigured_preserves_normal_admission(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    observe_runtime("anything")
    with runtime_admission("anything") as admitted:
        assert admitted


def test_restart_never_reinitializes_gate(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    gate.observe("worker")
    window = gate.request_drain()["window"]
    with pytest.raises(FileExistsError):
        MaintenanceGate.initialize(gate.directory, ["worker"])
    assert MaintenanceGate(gate.directory).status()["window"] == window
    with runtime_admission("worker") as admitted:
        assert not admitted


def test_roster_missing_blocks_false_drained(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("worker", "vector-cycle"))
    gate.observe("worker")
    window = gate.request_drain()["window"]
    assert gate.status()["state"] == "DRAINING"
    assert gate.status()["missing"] == ["vector-cycle"]
    with pytest.raises(GateError, match="GATE_NOT_DRAINED"):
        gate.claim_maintenance(window)
    with gate.admission("vector-cycle") as admitted:
        assert not admitted
    assert gate.wait_drained(window, 0)["state"] == "DRAINED"


def test_cross_process_drain_preserves_current_cycle(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    started, finish = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_child_cycle, args=(str(gate.directory), started, finish))
    process.start()
    try:
        assert started.wait(8)
        assert gate.status()["active"] == 1
        window = gate.request_drain()["window"]
        with gate.admission("worker") as admitted:
            assert not admitted
        assert gate.status()["state"] == "DRAINING"
        with pytest.raises(GateError, match="GATE_DRAIN_TIMEOUT"):
            gate.wait_drained(window, 0.05)
        with pytest.raises(GateError, match="GATE_RESUME_BLOCKED"):
            gate.resume(window)
        assert gate.status()["window"] == window
        finish.set()
        process.join(8)
        assert process.exitcode == 0
        assert gate.wait_drained(window, 1)["active"] == 0
        hold = gate.claim_maintenance(window)
        with pytest.raises(GateError, match="GATE_MAINTENANCE_ALREADY_HELD"):
            gate.claim_maintenance(window)
        with pytest.raises(GateError, match="GATE_RESUME_BLOCKED"):
            gate.resume(window)
        with pytest.raises(GateError, match="GATE_HOLD_MISMATCH"):
            gate.release_maintenance(window, uuid4().hex)
        gate.release_maintenance(window, hold)
        assert gate.status()["state"] == "DRAINED"
        gate.resume(window)
        with gate.admission("worker") as admitted:
            assert admitted
        assert gate.status()["state"] == "RUNNING"
        next_window = gate.request_drain()["window"]
        assert next_window != window
        with pytest.raises(GateError, match="GATE_WINDOW_MISMATCH"):
            gate.resume(window)
    finally:
        finish.set()
        if process.is_alive():
            process.join(2)
        if process.is_alive():
            process.terminate()
            process.join()


def test_abandoned_process_is_not_reaped(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_child_abandon, args=(str(gate.directory),))
    process.start()
    process.join(8)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("synthetic child did not finish")
    assert process.exitcode == 23
    reopened = MaintenanceGate(gate.directory)
    window = reopened.request_drain()["window"]
    assert reopened.status()["active"] == 1
    with pytest.raises(GateError, match="GATE_DRAIN_TIMEOUT"):
        reopened.wait_drained(window, 0)
    with pytest.raises(GateError, match="GATE_NOT_DRAINED"):
        reopened.claim_maintenance(window)
    with pytest.raises(GateError, match="GATE_RESUME_BLOCKED"):
        reopened.resume(window)


def test_failed_work_is_uncertain_and_not_reaped(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    calls = []
    with pytest.raises(RuntimeError, match="synthetic unknown result"):
        with gate.admission("worker") as admitted:
            assert admitted
            calls.append("external_attempt")
            raise RuntimeError("synthetic unknown result")
    window = gate.request_drain()["window"]
    assert gate.status()["active"] == 1 and gate.status()["uncertain"] == 1
    with gate.admission("worker") as admitted:
        assert not admitted
    assert calls == ["external_attempt"]
    with pytest.raises(GateError, match="GATE_DRAIN_TIMEOUT"):
        gate.wait_drained(window, 0)


def test_hold_survives_controller_reopen(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    gate.observe("worker")
    window = gate.request_drain()["window"]
    hold = gate.claim_maintenance(window)
    restarted = MaintenanceGate(gate.directory)
    assert restarted.status()["maintenance_held"]
    with pytest.raises(GateError, match="GATE_RESUME_BLOCKED"):
        restarted.resume(window)
    with restarted.admission("worker") as admitted:
        assert not admitted
    restarted.release_maintenance(window, hold)
    restarted.resume(window)
    assert restarted.status()["state"] == "RUNNING"


def test_cancel_before_any_work_after_missing_roster(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("worker", "not-running"))
    gate.observe("worker")
    window = gate.request_drain()["window"]
    with pytest.raises(GateError, match="GATE_DRAIN_TIMEOUT"):
        gate.wait_drained(window, 0)
    assert gate.resume(window)["state"] == "RUNNING"
    assert gate.status()["missing"] == ["not-running"]


@pytest.mark.parametrize("case", ["missing", "malformed", "duplicate", "bad_mode", "symlink", "lock_replaced"])
def test_corrupt_configured_storage_never_admits(tmp_path, monkeypatch, case):
    gate = make_gate(tmp_path, monkeypatch)
    state = gate.directory / "state.json"
    if case == "missing":
        state.unlink()
    elif case == "malformed":
        state.write_text("{}")
    elif case == "duplicate":
        state.write_text('{"version":1,"version":1}')
    elif case == "bad_mode":
        state.chmod(0o644)
    elif case == "symlink":
        target = tmp_path / "another.json"
        state.rename(target)
        state.symlink_to(target)
    else:
        lock = gate.directory / "control.lock"
        # Keep old inode alive, so replacement cannot reuse its number.
        lock.rename(gate.directory / "old.lock")
        lock.touch(mode=0o600)
    with pytest.raises(GateError):
        with runtime_admission("worker"):
            pytest.fail("corrupt gate admitted work")


def test_unknown_participant_refused(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    with pytest.raises(GateError, match="GATE_PARTICIPANT_NOT_ENROLLED"):
        with gate.admission("unlisted"):
            pytest.fail("unlisted participant admitted")
    assert gate.status()["active"] == 0


def test_empty_config_does_not_disable_gate(monkeypatch):
    monkeypatch.setenv(ENV, "")
    with pytest.raises(GateError, match="GATE_ABSOLUTE_PATH_REQUIRED"):
        with runtime_admission("worker"):
            pytest.fail("empty path admitted")


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


SCOPE = {"type": "http", "method": "POST", "path": "/synthetic", "headers": []}


def test_http_drained_returns_retryable_503(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("api-http",))
    gate.observe("api-http")
    window = gate.request_drain()["window"]
    sent = []

    async def app(*args):
        pytest.fail("closed gate invoked application")

    async def send(message):
        sent.append(message)

    asyncio.run(MaintenanceMiddleware(app, "api-http")(SCOPE, _receive, send))
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"2") in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"detail": "MAINTENANCE_UNAVAILABLE"}
    assert gate.wait_drained(window, 0)["active"] == 0


def test_http_cancellation_holds_application_reservation(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("api-http",))

    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()
        sent = []

        async def app(scope, receive, send):
            started.set()
            await finish.wait()
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"fixture"})

        async def send(message):
            sent.append(message)

        middleware = MaintenanceMiddleware(app, "api-http")
        task = asyncio.create_task(middleware(SCOPE, _receive, send))
        await asyncio.wait_for(started.wait(), 2)
        window = gate.request_drain()["window"]
        task.cancel()
        await asyncio.sleep(0)
        assert gate.status()["active"] == 1
        assert gate.status()["state"] == "DRAINING"
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert sent[0]["status"] == 202
        assert gate.wait_drained(window, 0)["active"] == 0

    asyncio.run(scenario())


def test_executor_cancellation_keeps_own_reservation(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("internal-ingress-executor",))
    started, finish = threading.Event(), threading.Event()
    effects = []

    def synthetic_ingress():
        started.set()
        assert finish.wait(5)
        effects.append("persist_once")
        return {"status": "accepted"}

    async def scenario():
        future = asyncio.get_running_loop().run_in_executor(None, guarded_ingress_call, synthetic_ingress)
        try:
            assert await asyncio.to_thread(started.wait, 2)
            window = gate.request_drain()["window"]
            future.cancel()
            with pytest.raises(asyncio.CancelledError):
                await future
            assert gate.status()["active"] == 1
            with pytest.raises(HTTPException) as refused:
                await asyncio.to_thread(guarded_ingress_call, synthetic_ingress)
            assert refused.value.status_code == 503
            finish.set()
            result = await asyncio.to_thread(gate.wait_drained, window, 3)
            assert result["active"] == 0 and effects == ["persist_once"]
        finally:
            finish.set()

    asyncio.run(scenario())


def test_expected_ingress_refusal_closes_reservation(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch, ("internal-ingress-executor",))

    def rejected():
        raise HTTPException(status_code=409, detail="synthetic conflict")

    with pytest.raises(HTTPException) as err:
        guarded_ingress_call(rejected)
    assert err.value.status_code == 409
    assert gate.status()["active"] == 0


def test_main_worker_closed_gate_never_probes_or_claims(tmp_path, monkeypatch):
    from attention_router.infrastructure import worker
    from attention_router.application import voice_backfill

    gate = make_gate(tmp_path, monkeypatch)
    window = gate.request_drain()["window"]

    class EndLoop(BaseException):
        pass

    def unexpected(*args, **kwargs):
        pytest.fail("closed gate called worker operation")

    def end_loop(*args, **kwargs):
        raise EndLoop

    monkeypatch.setattr(worker, "probe_transport_ready", unexpected)
    monkeypatch.setattr(worker, "SessionLocal", unexpected)
    monkeypatch.setattr(voice_backfill, "start_voice_backfill", lambda: None)
    monkeypatch.setattr(worker.time, "sleep", end_loop)
    with pytest.raises(EndLoop):
        worker.run_forever()
    assert gate.wait_drained(window, 0)["active"] == 0


def test_actual_vector_cycle_finishes_before_drain(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from attention_router.infrastructure import conversation_vector_runtime as vector

    gate = make_gate(tmp_path, monkeypatch, ("vector-cycle",))
    monkeypatch.setenv("CONVERSATION_VECTOR_ENABLED", "true")
    calls = []
    windows = []
    targets = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _seconds):
            self.stopped = True

    class Thread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    def cycle(*args, **kwargs):
        assert gate.status()["active"] == 1
        windows.append(gate.request_drain()["window"])
        assert gate.status()["state"] == "DRAINING"
        calls.append("cycle")

    monkeypatch.setattr(vector, "threading", SimpleNamespace(Event=Stop, Thread=Thread))
    monkeypatch.setattr(vector, "cycle", cycle)
    app = FastAPI()
    vector.install(app, SimpleNamespace(admin_auth_enabled=True, admin_token="fixture"),
                   lambda: None, lambda _: None)
    assert len(app.router.on_startup) == 1
    app.router.on_startup[0]()
    targets[0]()
    assert calls == ["cycle"]
    assert gate.wait_drained(windows[0], 0)["active"] == 0


def test_actual_backfill_denied_before_session(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from attention_router.application import voice_backfill
    from attention_router.infrastructure import db

    gate = make_gate(tmp_path, monkeypatch, ("voice-backfill",))
    window = gate.request_drain()["window"]
    monkeypatch.setenv("WHATSAPP_VOICE_BACKFILL_ENABLED", "true")
    targets = []

    class EndLoop(BaseException):
        pass

    def unexpected(*args, **kwargs):
        pytest.fail("closed gate attempted historical DB work")

    def end_loop(*args, **kwargs):
        raise EndLoop

    class Thread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(voice_backfill, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(db, "SessionLocal", unexpected)
    monkeypatch.setattr(voice_backfill.time, "sleep", end_loop)
    voice_backfill.start_voice_backfill()
    with pytest.raises(EndLoop):
        targets[0]()
    assert gate.wait_drained(window, 0)["active"] == 0


def test_neutral_ingress_maintenance_preserves_transport_contract(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from attention_router.web.ingress_app import create_ingress_app
    from attention_router.web import integration_ingress

    gate = make_gate(tmp_path, monkeypatch, ("neutral-ingress-http",))
    monkeypatch.setattr(integration_ingress, "admit_inbound",
                        lambda *a, **k: pytest.fail("closed gate reached database admission"))
    app = create_ingress_app()
    with TestClient(app) as client:
        window = gate.request_drain()["window"]
        r = client.post(integration_ingress.INTEGRATION_INGRESS_PATH, json={"fixture": True})
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "2" and r.headers["Cache-Control"] == "no-store"
        assert set(r.json()) == {"transport_version", "error_code", "request_id"}
        assert r.json()["error_code"] == "INGRESS_UNAVAILABLE"
        assert gate.wait_drained(window, 0)["state"] == "DRAINED"
        gate.resume(window)


def test_actual_internal_ingress_registers_executor_and_refuses_work(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from attention_router.web import internal_ingress_app as internal

    gate = make_gate(tmp_path, monkeypatch, ("internal-ingress-http", "internal-ingress-executor"))
    monkeypatch.setattr(internal, "process_internal_event",
                        lambda *a, **k: pytest.fail("closed ingress reached executor"))
    with TestClient(internal.create_internal_ingress_app()) as client:
        window = gate.request_drain()["window"]
        assert gate.wait_drained(window, 0)["missing"] == []
        assert client.post("/api/v1/ingress/internal/events", json={"fixture": True}).status_code == 503
        assert client.post("/internal/whatsapp/media", json={"fixture": True}).status_code == 503


def test_actual_context_reconciler_is_admission_guarded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from attention_router.infrastructure import google_contacts_runtime as runtime
    from attention_router.application import conversation_context

    gate = make_gate(tmp_path, monkeypatch, ("conversation-context-cycle",))
    monkeypatch.setenv("CONVERSATION_CONTEXT_ENABLED", "true")
    targets = []
    calls = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            self.stopped = True

    class Thread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    def unexpected(*args, **kwargs):
        calls.append("unexpected")
        pytest.fail("closed gate started reconciliation")

    monkeypatch.setattr(runtime, "threading", SimpleNamespace(Event=Stop, Thread=Thread))
    monkeypatch.setattr(runtime, "SessionLocal", unexpected)
    monkeypatch.setattr(conversation_context, "reconcile", unexpected)
    app = FastAPI()
    runtime.install_conversation_context(app)
    app.router.on_startup[0]()
    window = gate.request_drain()["window"]
    targets[0]()
    assert calls == []
    assert gate.wait_drained(window, 0)["active"] == 0


def test_standalone_artifact_adapter_declines_before_database(tmp_path, monkeypatch):
    from attention_router.infrastructure import standalone_artifact_worker as artifact

    gate = make_gate(tmp_path, monkeypatch, ("artifact-worker",))
    window = gate.request_drain()["window"]
    handlers = {}

    def end_loop(_seconds):
        handlers[artifact.signal.SIGTERM](None, None)

    monkeypatch.setattr(artifact.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(artifact, "SessionLocal", lambda: pytest.fail("closed gate reached DB"))
    monkeypatch.setattr(artifact.time, "sleep", end_loop)
    artifact.run()
    assert gate.wait_drained(window, 0)["active"] == 0


def test_gate_lock_contention_is_not_admission(tmp_path, monkeypatch):
    import fcntl

    gate = make_gate(tmp_path, monkeypatch)
    with (gate.directory / "control.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(GateError, match="GATE_LOCK_TIMEOUT"):
            with gate.admission("worker"):
                pytest.fail("contended gate admitted work")
    assert gate.status()["active"] == 0


def test_active_work_does_not_resume_after_crashed_controller(tmp_path, monkeypatch):
    gate = make_gate(tmp_path, monkeypatch)
    gate.observe("worker")
    old = gate.request_drain()["window"]
    hold = gate.claim_maintenance(old)
    # New controller instance must respect the durable hold.
    other = MaintenanceGate(gate.directory)
    with pytest.raises(GateError, match="GATE_OTHER_WINDOW_ACTIVE"):
        other.request_drain()
    assert other.status()["state"] == "DRAINED" and other.status()["maintenance_held"]
    other.release_maintenance(old, hold)
    other.resume(old)
    new = other.request_drain()["window"]
    with pytest.raises(GateError, match="GATE_HOLD_MISMATCH"):
        other.release_maintenance(new, hold)
