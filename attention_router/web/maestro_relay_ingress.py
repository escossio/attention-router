"""Opt-in relay receipt endpoint on the *existing* ingress application.

No new daemon, service, ticketing engine or authority. Disabled by default.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.maestro.admission import RELAY_AUDIENCE, admit_maestro_relay
from attention_router.maestro.relay_contract import MAX_RELAY_BODY_BYTES

MAESTRO_RELAY_PATH = "/api/v1/ingress/maestro/occurrences"


def get_maestro_relay_session_factory():
    return SessionLocal


def _raw_headers(request, name):
    return [value for key, value in request.scope.get("headers", []) if key.lower() == name]


def _response(code, status_code, **extra):
    return JSONResponse(
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
        content={"status": code, **extra},
    )


def build_maestro_relay_router() -> APIRouter:
    router = APIRouter()

    @router.post(MAESTRO_RELAY_PATH, tags=["maestro-relay"])
    async def accept_relay(request: Request,
                           session_factory=Depends(get_maestro_relay_session_factory)):
        if not settings.maestro_relay_ingress_enabled:
            return _response("UNAVAILABLE", 503)
        if request.url.query:
            return _response("INVALID_REQUEST", 400)
        # Reject HTTP ambiguity before body parsing.
        lengths = _raw_headers(request, b"content-length")
        auths = _raw_headers(request, b"authorization")
        keys = _raw_headers(request, b"idempotency-key")
        types = _raw_headers(request, b"content-type")
        headers = {k.lower() for k, _v in request.scope.get("headers", [])}
        if (
            len(lengths) != 1 or len(auths) > 1 or len(keys) != 1 or
            len(types) != 1 or
            b"transfer-encoding" in headers or b"content-encoding" in headers or
            b"x-tenant-id" in headers or b"x-attention-signature" in headers or
            b"x-api-key" in headers
        ):
            return _response("INVALID_REQUEST", 400)
        if types[0].lower().strip() != b"application/json":
            return _response("UNSUPPORTED_MEDIA_TYPE", 415)
        if not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_RELAY_BODY_BYTES:
            return _response("BODY_TOO_LARGE", 413)
        if not auths:
            return _response("UNAUTHENTICATED", 401)
        try:
            authorization = auths[0].decode("ascii")
            idempotency_key = keys[0].decode("ascii")
        except UnicodeDecodeError:
            return _response("UNAUTHENTICATED", 401)
        if not authorization.startswith("Bearer "):
            return _response("UNAUTHENTICATED", 401)
        token = authorization[len("Bearer "):]
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_RELAY_BODY_BYTES:
                return _response("BODY_TOO_LARGE", 413)
            chunks.append(chunk)
        if total != int(lengths[0]):
            return _response("INVALID_REQUEST", 400)
        result = await run_in_threadpool(
            admit_maestro_relay, session_factory, token, b"".join(chunks),
            idempotency_key=idempotency_key, audience=RELAY_AUDIENCE,
        )
        code = result.code
        if code in ("ACCEPTED", "DUPLICATE"):
            return _response(
                code, 202 if code == "ACCEPTED" else 200,
                receipt_id=result.receipt_id,
                occurrence_number=result.occurrence_number,
            )
        status_by_code = {
            "UNAUTHENTICATED": 401,
            "FORBIDDEN": 403,
            "INVALID_CONTRACT": 422,
            "CONFLICT": 409,
            "UNAVAILABLE": 503,
        }
        return _response(code, status_by_code.get(code, 503))

    return router
