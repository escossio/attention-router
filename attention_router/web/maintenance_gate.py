"""ASGI adapter for the optional local maintenance gate.

A request keeps its reservation through response completion and dependency
cleanup. A cancelled caller does not release a detached synchronous executor
operation. Existing response semantics are untouched while unconfigured.
"""

import asyncio

import anyio

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from attention_router.infrastructure.maintenance_gate import (
    GateError,
    configured_gate,
    runtime_admission,
)


class MaintenanceMiddleware:
    def __init__(self, app, participant, unavailable_response=None):
        self.app = app
        self.participant = participant
        self.unavailable_response = unavailable_response

    async def __call__(self, scope, receive, send):
        try:
            gate = configured_gate()
        except GateError:
            if scope["type"] == "http":
                await self._unavailable(scope, receive, send)
                return
            raise
        if gate is None or scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No websocket routes are part of this adapter's contract.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            raise GateError("GATE_SCOPE_UNSUPPORTED")
        cancelled = False
        admitted = False
        try:
            with gate.admission(self.participant) as admitted:
                if admitted:
                    task = asyncio.create_task(self.app(scope, receive, send))
                    # Do not abandon application work on client/server cancellation.
                    # If the inner task itself fails, admission remains uncertain.
                    while True:
                        try:
                            with anyio.CancelScope(shield=True):
                                await asyncio.shield(task)
                            break
                        except asyncio.CancelledError:
                            cancelled = True
                            if task.done():
                                task.result()
                                break
        except GateError:
            if admitted:
                # A completion/ledger error after the app may follow a sent response.
                # Never send a second response or claim the work finished safely.
                raise
            await self._unavailable(scope, receive, send)
            return
        if not admitted:
            await self._unavailable(scope, receive, send)
            return
        if cancelled:
            raise asyncio.CancelledError

    async def _unavailable(self, scope, receive, send):
        response = self.unavailable_response(scope) if self.unavailable_response else None
        if response is None:
            response = JSONResponse(
                {"detail": "MAINTENANCE_UNAVAILABLE"}, status_code=503,
                headers={"Retry-After": "2", "Cache-Control": "no-store"},
            )
        await response(scope, receive, send)


def install_maintenance_middleware(app, participant, *, extra_participants=(), unavailable_response=None):
    app.add_middleware(MaintenanceMiddleware, participant=participant, unavailable_response=unavailable_response)

    @app.on_event("startup")
    def observe():
        gate = configured_gate()
        if gate is not None:
            gate.observe(participant)
            for extra in extra_participants:
                gate.observe(extra)


def guarded_ingress_call(function, *args):
    """The reservation belongs to the executor thread, not its awaiting future."""
    expected_error = None
    with runtime_admission("internal-ingress-executor") as admitted:
        if not admitted:
            raise HTTPException(status_code=503, detail="MAINTENANCE_UNAVAILABLE")
        try:
            return function(*args)
        except HTTPException as exc:
            # Existing ingress functions roll back/close before raising these
            # expected HTTP errors. Propagate after the reservation is released.
            expected_error = exc
    raise expected_error
