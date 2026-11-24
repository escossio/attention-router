"""Loopback-only actual TCP/HTTP canary for the opt-in staged ASGI boundary.

Synthetic authority and bytes only; never contacts external services.
"""
from contextlib import contextmanager
from hashlib import sha256
from threading import Thread
from time import monotonic, sleep
import socket

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from attention_router.api.v1.client_context_staged import StagedContextEndpoint
from attention_router.application.staged_read import (
    AuthorizedSnapshot, DetachedEvidence, SourceVersion, StagedRead, StagedReadRejected,
)
from attention_router.config import settings


class Authority:
    def __init__(self):
        self.revision = "1"
        self.inside = False

    @contextmanager
    def capture(self, *, bearer, scope_ref, question):
        assert bearer == "synthetic-bearer"
        assert not self.inside
        self.inside = True
        try:
            yield AuthorizedSnapshot(
                scope_ref=scope_ref,
                question_digest=sha256(question.encode()).hexdigest(),
                consent_revision=self.revision,
                epoch_token="synthetic-epoch",
                evidence=DetachedEvidence(b"synthetic-input", (
                    SourceVersion("synthetic-source", "v1", "synthetic-receipt", "v1", "synthetic-hash"),
                )),
            )
        finally:
            self.inside = False

    @contextmanager
    def delivery_gate(self, *, bearer, scope_ref, question, snapshot):
        assert not self.inside
        self.inside = True
        try:
            if snapshot.consent_revision != self.revision:
                raise StagedReadRejected("SYNTHETIC_REVOCATION")
            yield
        finally:
            self.inside = False


@pytest.mark.parametrize("revoke", [False, True])
def test_staged_http_loopback_only(monkeypatch, revoke):
    monkeypatch.setattr(settings, "app_env", "test")
    authority = Authority()

    def compute(evidence, question):
        assert not authority.inside
        assert evidence.content == b"synthetic-input"
        if revoke:
            authority.revision = "2"
        return b'{"answer":"synthetic-ok"}'

    reader = StagedRead(authority, compute)
    app = Starlette(routes=[Route(
        "/api/v1/client/context/staged-evidence",
        StagedContextEndpoint(reader), methods=["POST"],
    )])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="critical",
        access_log=False, lifespan="off", timeout_keep_alive=1,
    ))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not server.started and monotonic() < deadline:
            sleep(0.02)
        assert server.started, "loopback ASGI server did not start"

        with httpx.Client(timeout=4, trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/api/v1/client/context/staged-evidence",
                json={"scope_ref": "ctx_" + "0" * 32, "question": "Synthetic question?"},
                headers={"Authorization": "Bearer synthetic-bearer"},
            )
            if revoke:
                assert response.status_code == 503
                assert response.content == b""
            else:
                assert response.status_code == 200
                assert response.json() == {"answer": "synthetic-ok"}
                assert response.headers["cache-control"] == "no-store"

            unauthorized = client.post(
                f"http://127.0.0.1:{port}/api/v1/client/context/staged-evidence",
                json={"scope_ref": "ctx_" + "0" * 32, "question": "Synthetic question?"},
            )
            assert unauthorized.status_code == 401
            assert unauthorized.content == b""
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
