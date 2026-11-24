"""Fast contract checks for staged read, before PostgreSQL/HTTP integration."""

from contextlib import contextmanager
from hashlib import sha256
from time import sleep

import pytest

from attention_router.application.staged_read import (
    AuthorizedSnapshot, DetachedEvidence, SourceVersion, StagedRead,
    StagedReadRejected,
)


def source(revision="v1"):
    return SourceVersion("source-1", revision, "receipt-1", revision, "hash-1")


class Authority:
    def __init__(self):
        self.inside = False
        self.revision = "1"
        self.source_revision = "v1"
        self.events = []
        self.override_scope = False

    @contextmanager
    def capture(self, *, bearer, scope_ref, question):
        assert bearer == "authenticated"
        assert not self.inside
        self.inside = True
        self.events.append("capture")
        try:
            yield AuthorizedSnapshot(
                scope_ref="wrong" if self.override_scope else scope_ref,
                question_digest=sha256(question.encode()).hexdigest(),
                consent_revision=self.revision,
                epoch_token="epoch-1",
                evidence=DetachedEvidence(b'{"evidence":"private"}', (source(self.source_revision),)),
            )
        finally:
            self.inside = False

    @contextmanager
    def delivery_gate(self, *, bearer, scope_ref, question, snapshot):
        assert bearer == "authenticated"
        assert not self.inside
        self.inside = True
        self.events.append("final")
        try:
            if snapshot.consent_revision != self.revision:
                raise StagedReadRejected("revoked")
            if snapshot.evidence.sources[0].revision != self.source_revision:
                raise StagedReadRejected("source_changed")
            yield
        finally:
            self.inside = False


def run(authority, compute, deliver):
    return StagedRead(authority, compute).query_and_deliver(
        bearer="authenticated", scope_ref="scope-1",
        question="Quando?", deliver=deliver,
    )


def test_compute_outside_lock_but_delivery_inside_final_gate():
    authority = Authority()
    output = []

    def compute(evidence, question):
        assert not authority.inside
        assert evidence.content == b'{"evidence":"private"}'
        authority.events.append("compute")
        return b'{"answer":"ok"}'

    def deliver(body):
        assert authority.inside
        authority.events.append("deliver")
        output.append(body)

    receipt = run(authority, compute, deliver)
    assert authority.events == ["capture", "compute", "final", "deliver"]
    assert output == [b'{"answer":"ok"}']
    assert receipt.body_sha256 == sha256(output[0]).hexdigest()
    assert receipt.body_size == len(output[0])
    assert not authority.inside


def test_revocation_during_compute_prevents_disclosure():
    authority = Authority()
    seen = []

    def compute(*_):
        assert not authority.inside
        authority.revision = "2"
        return b"no-disclose"

    with pytest.raises(StagedReadRejected, match="revoked"):
        run(authority, compute, seen.append)
    assert seen == [] and not authority.inside


def test_source_revision_change_prevents_disclosure():
    authority = Authority()
    seen = []

    def compute(*_):
        authority.source_revision = "v2"
        return b"no-disclose"

    with pytest.raises(StagedReadRejected, match="source_changed"):
        run(authority, compute, seen.append)
    assert seen == [] and not authority.inside


def test_scope_mismatch_fails_before_compute():
    authority = Authority()
    authority.override_scope = True
    seen = []

    with pytest.raises(StagedReadRejected, match="SCOPE_MISMATCH"):
        run(authority, lambda *_: seen.append("compute"), seen.append)
    assert seen == [] and not authority.inside


def test_non_bytes_and_large_response_rejected_without_delivery():
    authority = Authority()
    seen = []
    for response in ("unsafe", b"x" * 262145):
        with pytest.raises(StagedReadRejected, match="RESPONSE_INVALID"):
            run(authority, lambda *_: response, seen.append)
    assert seen == []


def test_expired_compute_does_not_enter_delivery_gate():
    authority = Authority()
    seen = []
    pipeline = StagedRead(authority, lambda *_: (sleep(0.02), b"ok")[1],
                          compute_budget_seconds=0.001)
    with pytest.raises(StagedReadRejected, match="COMPUTE_EXPIRED"):
        pipeline.query_and_deliver(bearer="authenticated", scope_ref="scope-1",
                                   question="Quando?", deliver=seen.append)
    assert authority.events == ["capture"] and seen == []


def test_transport_error_propagates_and_releases_authority():
    authority = Authority()
    def failure(_):
        raise OSError("network_failed")
    with pytest.raises(OSError, match="network_failed"):
        run(authority, lambda *_: b"ok", failure)
    assert not authority.inside


def test_incomplete_or_duplicate_dependency_rejected():
    with pytest.raises(StagedReadRejected, match="DEPS_INCOMPLETE"):
        DetachedEvidence(b"x", (SourceVersion("s", "", "r", "v", "h"),))
    with pytest.raises(StagedReadRejected, match="DEPS_DUPLICATE"):
        DetachedEvidence(b"x", (source(), source()))


def test_every_request_newly_captures_authority_no_cross_request_cache():
    authority = Authority()
    seen = []
    for _ in range(3):
        run(authority, lambda *_: b"ok", seen.append)
    assert authority.events.count("capture") == 3
    assert authority.events.count("final") == 3
    assert seen == [b"ok"] * 3


@pytest.mark.parametrize("before_send", [True, False])
def test_asgi_revocation_order(tmp_path, monkeypatch, before_send):
    import asyncio
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta
    from threading import Event

    from attention_router.application.context_consent import RETENTION_LAYERS, LabConsent, SourceConsent
    from attention_router.config import settings

    monkeypatch.setattr(settings, "app_env", "test")
    now = datetime.now(UTC)
    deadline = now + timedelta(days=1)
    consent = LabConsent(tmp_path / "consent.sqlite", (SourceConsent(
        "decision", "tenant", "owner", "account", "thread", "source-1", "receipt-1",
        "prospective_context", now, deadline, "NORMAL", tuple((key, deadline) for key in RETENTION_LAYERS),
    ),))
    consent.initialize()
    events = []
    writer_contended = Event()

    class ConsentAuthority(Authority):
        @contextmanager
        def capture(self, **kwargs):
            with consent.current() as (self.revision, _), super().capture(**kwargs) as snapshot:
                yield snapshot

        @contextmanager
        def delivery_gate(self, *, snapshot, **kwargs):
            with consent.current(snapshot.consent_revision), super().delivery_gate(snapshot=snapshot, **kwargs):
                yield

    def revoke():
        # Prove the real SQLite write gate is occupied before submitting revoke.
        with sqlite3.connect(consent.path, timeout=0) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        writer_contended.set()
        assert consent.revoke("source-1")
        events.append("revocation_committed")

    async def exercise():
        authority = ConsentAuthority()
        def compute(*_):
            if before_send:
                assert consent.revoke("source-1")
                events.append("revocation_committed")
            return b'{"evidence":"private"}'

        pipeline = StagedRead(authority, compute)
        task, _, messages, body_started, release = await start_endpoint(pipeline, events=events)
        if before_send:
            await asyncio.wait_for(task, 3)
            assert events == ["revocation_committed"]
            assert messages[0]["status"] == 503
            assert messages[1]["body"] == b""
            return
        await asyncio.wait_for(body_started.wait(), 3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(revoke)
            async with asyncio.timeout(1):
                while not writer_contended.is_set():
                    await asyncio.sleep(0.01)
            assert not writer.done()
            assert events == []
            release.set()
            await asyncio.wait_for(task, 3)
            async with asyncio.timeout(5):
                while not writer.done():
                    await asyncio.sleep(0.01)
            writer.result()
        assert events == ["body_send_returned", "revocation_committed"]
        assert messages[0]["status"] == 200
        assert (b"cache-control", b"no-store") in messages[0]["headers"]
        assert messages[1] == {
            "type": "http.response.body", "body": b'{"evidence":"private"}', "more_body": False,
        }

    asyncio.run(exercise())


async def start_endpoint(pipeline, *, fail=None, events=None):
    import asyncio

    from attention_router.api.v1.client_context_staged import StagedContextEndpoint

    inbox = asyncio.Queue()
    inbox.put_nowait({
        "type": "http.request",
        "body": b'{"question":"Quando?","scope_ref":"ctx_00000000000000000000000000000000"}',
    })
    messages = []
    body_started = asyncio.Event()
    release = asyncio.Event()

    async def send(message):
        if message.get("body"):
            body_started.set()
            await release.wait()
            if fail:
                raise fail
            if events is not None:
                events.append("body_send_returned")
        messages.append(message)

    async def run():
        # The offline sandbox can deny asyncio's socketpair wake writes.
        # A timer keeps the test loop responsive to worker callbacks there.
        loop = asyncio.get_running_loop()
        tick = None

        def wake():
            nonlocal tick
            tick = loop.call_later(0.01, wake)

        wake()
        try:
            await StagedContextEndpoint(pipeline)(
                {"type": "http", "headers": [(b"authorization", b"Bearer authenticated")]},
                inbox.get, send,
            )
        finally:
            tick.cancel()

    task = asyncio.create_task(run())
    return task, inbox, messages, body_started, release


@pytest.mark.parametrize("failure", ["revoked", "fraud", "disconnect", "exception", "cancel"])
def test_asgi_fails_closed(monkeypatch, failure, caplog):
    import asyncio

    from attention_router.config import settings

    monkeypatch.setattr(settings, "app_env", "test")
    authority = Authority()

    def compute(*_):
        if failure == "revoked":
            authority.revision = "revocation_committed"
        return b'{"evidence":"private"}'

    authority.override_scope = failure == "fraud"

    async def exercise():
        task, inbox, messages, body_started, release = await start_endpoint(
            StagedRead(authority, compute),
            fail=RuntimeError("private") if failure == "exception" else None,
        )
        if failure in {"disconnect", "exception", "cancel"}:
            await asyncio.wait_for(body_started.wait(), 3)
            assert authority.inside
            if failure == "disconnect":
                inbox.put_nowait({"type": "http.disconnect"})
            elif failure == "cancel":
                task.cancel()
            else:
                release.set()
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
        assert not authority.inside
        assert not any(message.get("body") for message in messages)
        if failure in {"revoked", "fraud"}:
            assert messages[0]["status"] == 503
            assert (b"cache-control", b"no-store") in messages[0]["headers"]
        else:
            assert len(messages) == 1

    asyncio.run(exercise())
    assert "private" not in caplog.text
