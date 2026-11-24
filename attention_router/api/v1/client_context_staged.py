"""Standalone ASGI endpoint for the staged context test application."""

from collections.abc import Callable
from time import monotonic_ns
from typing import Annotated, Protocol

import anyio
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.requests import ClientDisconnect, Request
from starlette.types import Receive, Scope, Send

from attention_router.config import settings
from attention_router.observability.context_performance import (
    Outcome, Stage, classify, emit, observe_query, phase, request_sample, result,
)
from attention_router.observability.private_context import Decision, query_trace, record, record_failure
from attention_router.application.context_canary_gate import canary_configured


class ContextQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_ref: str = Field(pattern=r"^ctx_[A-Za-z0-9_-]{32,96}$")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class StagedDelivery(Protocol):
    def query_and_deliver(
        self, *, bearer: str, scope_ref: str, question: str, deliver: Callable[[bytes], None],
    ) -> object: ...


class StagedContextEndpoint:
    def __init__(self, reader: StagedDelivery):
        if not canary_configured():
            raise ValueError("STAGED_CONTEXT_PILOT_ONLY")
        self._reader = reader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        parent = None
        if settings.context_performance_enabled and (
            (scope.get("client") or (None,))[0] in ("127.0.0.1", "::1")
            and (scope.get("server") or (None,))[0] in ("127.0.0.1", "::1")
        ):
            parent = next((value.decode("ascii", errors="replace") for key, value in scope.get("headers", ())
                           if key == b"traceparent" and len(value) == 55), None)
        with query_trace() as correlation, observe_query(correlation, parent):
            await self._handle(scope, receive, send, correlation)

    async def _handle(self, scope, receive, send, correlation):
        # This route is never mounted by the normal application. Production
        # canary traffic is accepted only via a direct loopback socket.
        if settings.app_env != "test" and (
            not canary_configured()
            or (scope.get("client") or (None,))[0] not in ("127.0.0.1", "::1")
            or (scope.get("server") or (None,))[0] not in ("127.0.0.1", "::1")
        ):
            result(Outcome.AUTHORITY)
            await send({
                "type": "http.response.start", "status": 404,
                "headers": [(b"cache-control", b"no-store")],
            })
            await send({"type": "http.response.body", "body": b""})
            return
        request = Request(scope, receive)
        headers = [(b"cache-control", b"no-store"), (b"x-correlation-id", correlation.encode())]

        async def empty(status):
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b""})

        try:
            with phase(Stage.INPUT):
                raw = bytearray()
                with anyio.fail_after(2):
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > 8192:
                            raise ValueError("STAGED_REQUEST_TOO_LARGE")
                        raw.extend(chunk)
                payload = ContextQuestion.model_validate_json(raw)
        except ClientDisconnect:
            result(Outcome.DISCONNECT)
            record(Decision.DISCONNECTED)
            return
        except (ValueError, TimeoutError) as error:
            result(Outcome.APP_TIMEOUT if isinstance(error, TimeoutError) else Outcome.INVALID)
            record_failure(error)
            record(Decision.REFUSED)
            with anyio.move_on_after(2):
                try:
                    await empty(422)
                except Exception:
                    pass
            return

        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        finished = anyio.Event()
        send_scope = anyio.CancelScope()
        started = False
        status = 503
        queued_at = 0
        sample = request_sample.get()

        async def send_body(body: bytes):
            nonlocal started
            with send_scope, phase(Stage.SEND):
                with anyio.fail_after(2):
                    await anyio.lowlevel.checkpoint()
                    record(Decision.ALLOWED)
                    started = True
                    await send({
                        "type": "http.response.start", "status": 200,
                        "headers": [*headers, (b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())],
                    })
                    await anyio.lowlevel.checkpoint()
                    await send({"type": "http.response.body", "body": body, "more_body": False})
                    record(Decision.SENT)
                    if sample is not None:
                        sample.delivered = True
                        result(sample.answer)
            if send_scope.cancel_called:
                raise OSError("STAGED_SEND_CANCELLED")

        def read():
            if sample is not None:
                emit("end", Stage.EXECUTOR_WAIT, duration=monotonic_ns() - queued_at)
            try:
                with phase(Stage.WORKER):
                    self._reader.query_and_deliver(
                        bearer=token, scope_ref=payload.scope_ref, question=payload.question,
                        deliver=lambda body: anyio.from_thread.run(send_body, body),
                    )
            finally:
                if sample is not None and sample.disconnected is not None:
                    emit("end", Stage.AFTER_DISCONNECT,
                         duration=monotonic_ns() - sample.disconnected)

        async def run_read():
            nonlocal status, queued_at
            try:
                if scheme.lower() != "bearer" or not token:
                    result(Outcome.AUTHORITY)
                    record(Decision.REFUSED)
                    status = 401
                    return
                if sample is not None:
                    queued_at = monotonic_ns()
                    emit("begin", Stage.EXECUTOR_WAIT)
                    limiter = anyio.to_thread.current_default_thread_limiter().statistics()
                    emit("executor_capacity", Stage.EXECUTOR_WAIT, capacity=(
                        limiter.borrowed_tokens, limiter.total_tokens, limiter.tasks_waiting,
                    ))
                await anyio.to_thread.run_sync(read)
            except Exception as error:
                if sample is not None:
                    result(classify(error))
                record_failure(error)
                record(Decision.BLOCKED)
                # An exception may contain SQL parameters or protected text.
                status = 503
            finally:
                finished.set()

        async def watch_disconnect():
            try:
                while (await receive())["type"] != "http.disconnect":
                    pass
                if sample is not None and not sample.delivered and not finished.is_set():
                    sample.disconnected = monotonic_ns()
                    result(Outcome.DISCONNECT)
                    emit("disconnect", Stage.REQUEST, outcome=Outcome.DISCONNECT)
            except Exception:
                pass
            send_scope.cancel()

        async with anyio.create_task_group() as group:
            group.start_soon(watch_disconnect)
            group.start_soon(run_read)
            try:
                await finished.wait()
                if not started and not send_scope.cancel_called:
                    with send_scope, anyio.move_on_after(2):
                        try:
                            await empty(status)
                        except Exception:
                            pass
            finally:
                send_scope.cancel()
                group.cancel_scope.cancel()
