"""Standalone ASGI endpoint for the staged context test application."""

from collections.abc import Callable
from typing import Annotated, Protocol

import anyio
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.requests import ClientDisconnect, Request
from starlette.types import Receive, Scope, Send

from attention_router.config import settings
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
        with query_trace() as correlation:
            await self._handle(scope, receive, send, correlation)

    async def _handle(self, scope, receive, send, correlation):
        # This route is never mounted by the normal application. Production
        # canary traffic is accepted only via a direct loopback socket.
        if settings.app_env != "test" and (
            not canary_configured()
            or (scope.get("client") or (None,))[0] not in ("127.0.0.1", "::1")
            or (scope.get("server") or (None,))[0] not in ("127.0.0.1", "::1")
        ):
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
            raw = bytearray()
            with anyio.fail_after(2):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 8192:
                        raise ValueError("STAGED_REQUEST_TOO_LARGE")
                    raw.extend(chunk)
            payload = ContextQuestion.model_validate_json(raw)
        except ClientDisconnect:
            record(Decision.DISCONNECTED)
            return
        except (ValueError, TimeoutError) as error:
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

        async def send_body(body: bytes):
            nonlocal started
            with send_scope:
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
            if send_scope.cancel_called:
                raise OSError("STAGED_SEND_CANCELLED")

        def read():
            self._reader.query_and_deliver(
                bearer=token, scope_ref=payload.scope_ref, question=payload.question,
                deliver=lambda body: anyio.from_thread.run(send_body, body),
            )

        async def run_read():
            nonlocal status
            try:
                if scheme.lower() != "bearer" or not token:
                    record(Decision.REFUSED)
                    status = 401
                    return
                await anyio.to_thread.run_sync(read)
            except Exception as error:
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
