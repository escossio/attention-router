from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from attention_router.config import settings
from attention_router.domain.models import new_id
from attention_router.infrastructure.db import SessionLocal
from attention_router.integrations.admission import AdmissionResult, admit_inbound
from attention_router.integrations.tenant_binding import credential_digest


INTEGRATION_INGRESS_PATH = "/api/v1/ingress/integrations/events"
INTEGRATION_TRANSPORT_VERSION = "1"
INTEGRATION_MAX_BODY_BYTES = 65_536

# These headers belong to other Attention Router authority surfaces. They never
# authenticate the neutral integration ingress, and mixing them with bearer auth
# is rejected rather than silently selecting one authority source.
_ALTERNATE_AUTH_HEADERS = frozenset(
    {
        b"x-attention-signature",
        b"x-attention-timestamp",
        b"x-hub-signature-256",
        b"x-admin-token",
        b"x-api-key",
    }
)


def get_integration_session_factory():
    return SessionLocal


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    return [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == name
    ]


def _headers_present(request: Request, names: set[bytes] | frozenset[bytes]) -> bool:
    present = {key.lower() for key, _value in request.scope.get("headers", [])}
    return bool(present & names)


def _base_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


def _error(
    *,
    request_id: str,
    error_code: str,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    merged = _base_headers()
    if headers:
        merged.update(headers)
    return JSONResponse(
        status_code=status_code,
        headers=merged,
        content={
            "transport_version": INTEGRATION_TRANSPORT_VERSION,
            "error_code": error_code,
            "request_id": request_id,
        },
    )


def _success(result: AdmissionResult) -> JSONResponse:
    if result.receipt_id is None or result.admitted_at is None or result.correlation_id is None:
        raise ValueError("INTEGRATION_ADMISSION_SUCCESS_INCOMPLETE")
    admitted_at = result.admitted_at
    if admitted_at.tzinfo is None:
        admitted_at = admitted_at.replace(tzinfo=UTC)
    else:
        admitted_at = admitted_at.astimezone(UTC)
    duplicate = result.code == "DUPLICATE"
    return JSONResponse(
        status_code=200 if duplicate else 202,
        headers=_base_headers(),
        content={
            "transport_version": INTEGRATION_TRANSPORT_VERSION,
            "status": "duplicate" if duplicate else "accepted",
            "receipt_id": result.receipt_id,
            "admitted_at": admitted_at.isoformat().replace("+00:00", "Z"),
            "correlation_id": result.correlation_id,
        },
    )


def _content_type_supported(request: Request) -> bool:
    values = _raw_header_values(request, b"content-type")
    if len(values) != 1:
        return False
    try:
        raw = values[0].decode("ascii").strip().lower()
    except UnicodeDecodeError:
        return False
    pieces = [piece.strip() for piece in raw.split(";")]
    if pieces[0] != "application/json":
        return False
    if len(pieces) == 1:
        return True
    return len(pieces) == 2 and pieces[1] == "charset=utf-8"


def _extract_bearer(request: Request) -> tuple[str | None, str | None]:
    values = _raw_header_values(request, b"authorization")
    if len(values) > 1:
        return None, "AMBIGUOUS"
    alternate = _headers_present(request, _ALTERNATE_AUTH_HEADERS)
    if values and alternate:
        return None, "AMBIGUOUS"
    if not values:
        return None, "MISSING"
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None, "INVALID"
    if not value.startswith("Bearer "):
        return None, "INVALID"
    token = value[len("Bearer "):]
    try:
        credential_digest(token)
    except ValueError:
        return None, "INVALID"
    return token, None


async def _bounded_body(request: Request) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > INTEGRATION_MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _admission_error(
    *,
    request_id: str,
    result: AdmissionResult,
    token_supplied: bool,
) -> JSONResponse:
    mapping = {
        "INVALID_REQUEST": (400, "INVALID_REQUEST"),
        "UNAUTHENTICATED": (401, "UNAUTHENTICATED"),
        "BINDING_FORBIDDEN": (403, "BINDING_FORBIDDEN"),
        "IDEMPOTENCY_CONFLICT": (409, "IDEMPOTENCY_CONFLICT"),
        "BODY_TOO_LARGE": (413, "BODY_TOO_LARGE"),
        "INVALID_CONTRACT": (422, "INVALID_CONTRACT"),
        "INGRESS_UNAVAILABLE": (503, "INGRESS_UNAVAILABLE"),
    }
    status_code, error_code = mapping.get(
        result.code,
        (503, "INGRESS_UNAVAILABLE"),
    )
    headers: dict[str, str] = {}
    if status_code == 401:
        challenge = 'Bearer realm="andy-integration-ingress"'
        if token_supplied:
            challenge += ', error="invalid_token"'
        headers["WWW-Authenticate"] = challenge
    return _error(
        request_id=request_id,
        error_code=error_code,
        status_code=status_code,
        headers=headers,
    )


def build_integration_ingress_router() -> APIRouter:
    router = APIRouter()

    @router.post(INTEGRATION_INGRESS_PATH, tags=["integration-ingress"])
    async def integration_events(
        request: Request,
        session_factory: Any = Depends(get_integration_session_factory),
    ) -> JSONResponse:
        request_id = new_id()
        if not settings.integration_ingress_enabled:
            return _error(
                request_id=request_id,
                error_code="INGRESS_UNAVAILABLE",
                status_code=503,
            )

        # Ambiguous alternate authority/input selectors are rejected before auth.
        if request.url.query:
            return _error(
                request_id=request_id,
                error_code="INVALID_REQUEST",
                status_code=400,
            )
        if _headers_present(
            request,
            frozenset({b"idempotency-key", b"x-tenant-id"}),
        ):
            return _error(
                request_id=request_id,
                error_code="INVALID_REQUEST",
                status_code=400,
            )
        if len(_raw_header_values(request, b"content-length")) > 1:
            return _error(
                request_id=request_id,
                error_code="INVALID_REQUEST",
                status_code=400,
            )
        if _raw_header_values(request, b"content-encoding"):
            return _error(
                request_id=request_id,
                error_code="UNSUPPORTED_MEDIA_TYPE",
                status_code=415,
            )
        if not _content_type_supported(request):
            return _error(
                request_id=request_id,
                error_code="UNSUPPORTED_MEDIA_TYPE",
                status_code=415,
            )

        token, auth_error = _extract_bearer(request)
        if auth_error == "AMBIGUOUS":
            return _error(
                request_id=request_id,
                error_code="INVALID_REQUEST",
                status_code=400,
            )

        body = await _bounded_body(request)
        if body is None:
            return _error(
                request_id=request_id,
                error_code="BODY_TOO_LARGE",
                status_code=413,
            )

        # Authentication takes precedence over private body/schema diagnostics.
        if token is None:
            return _error(
                request_id=request_id,
                error_code="UNAUTHENTICATED",
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="andy-integration-ingress"'
                        + (
                            ', error="invalid_token"'
                            if auth_error == "INVALID"
                            else ""
                        )
                    )
                },
            )

        result = admit_inbound(
            session_factory,
            token,
            body,
            audience=settings.integration_ingress_audience,
        )
        if result.code in {"ACCEPTED", "DUPLICATE"}:
            try:
                return _success(result)
            except ValueError:
                return _error(
                    request_id=request_id,
                    error_code="INGRESS_UNAVAILABLE",
                    status_code=503,
                )
        return _admission_error(
            request_id=request_id,
            result=result,
            token_supplied=True,
        )

    @router.api_route(
        INTEGRATION_INGRESS_PATH,
        methods=["GET", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def integration_events_method_not_allowed(_request: Request) -> JSONResponse:
        return _error(
            request_id=new_id(),
            error_code="METHOD_NOT_ALLOWED",
            status_code=405,
            headers={"Allow": "POST"},
        )

    return router


__all__ = [
    "INTEGRATION_INGRESS_PATH",
    "INTEGRATION_MAX_BODY_BYTES",
    "INTEGRATION_TRANSPORT_VERSION",
    "build_integration_ingress_router",
    "get_integration_session_factory",
]
