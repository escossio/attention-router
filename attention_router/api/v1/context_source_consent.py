"""Explicit source decisions, mounted by the laboratory app factory only."""

from datetime import datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from attention_router.application.client_context import ClientContextReader, ClientContextScope
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionUnauthenticated,
)
from attention_router.application.context_pg_consent import PgSourceConsentWriter
from attention_router.application.context_owner_scope import AuthenticatedOwnerScopeRegistry
from attention_router.application.context_preactivation import require_context_database
from attention_router.config import settings
from attention_router.application.context_canary_gate import canary_configured


class ScopeEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: Annotated[str, Field(min_length=1, max_length=64)]


class RevokeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_ref: Annotated[str, Field(pattern=r"^ctx_[A-Za-z0-9_-]{32,96}$")]
    message_id: Annotated[str, Field(min_length=1, max_length=64)]


class GrantDecision(RevokeDecision):
    decision_ref: Annotated[str, Field(pattern=r"^[A-Za-z0-9_:-]{1,120}$")]
    expires_at: AwareDatetime
    retention: Annotated[dict[str, AwareDatetime], Field(min_length=7, max_length=7)]


def _enabled():
    return (
        canary_configured() and settings.context_source_consent_api_enabled
        and settings.client_session_enabled
    )


def mount_context_source_consent_for_test(
    app: FastAPI, *, writer: PgSourceConsentWriter,
    owner_scope_registry: AuthenticatedOwnerScopeRegistry | None = None,
    capture_start: datetime | None = None,
) -> bool:
    """The production app has no dependency wiring for this authority."""
    if not _enabled() or not isinstance(writer, PgSourceConsentWriter):
        return False
    reader = writer._reader
    if (
        not isinstance(reader, ClientContextReader) or not reader._role
        or not callable(getattr(reader._sessions, "begin", None)) or not callable(reader._bootstrap)
        or not all(isinstance(scope, ClientContextScope) for scope in reader._scopes.values())
        or (settings.app_env == "test" and not reader._scopes)
        or (settings.app_env != "test" and (reader._scopes or owner_scope_registry is None))
    ):
        return False
    if owner_scope_registry is not None and (
        not isinstance(owner_scope_registry, AuthenticatedOwnerScopeRegistry)
        or writer._owner_scope_registry is not owner_scope_registry
        or capture_start is None or capture_start.utcoffset() is None
    ):
        return False
    router = APIRouter(prefix="/api/v1/context/source-consent")

    async def decide(request, payload_type, operation, *, returns_scope=False):
        def empty(status):
            return Response(status_code=status, headers={"Cache-Control": "no-store"})

        if not _enabled():
            return empty(404)
        if settings.app_env != "test" and (
            (request.scope.get("client") or (None,))[0] not in ("127.0.0.1", "::1")
            or (request.scope.get("server") or (None,))[0] not in ("127.0.0.1", "::1")
        ):
            return empty(404)
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not bearer or len(bearer) > 128:
            return empty(401)
        # Parse manually so validation errors never echo values from the request.
        try:
            raw = bytearray()
            with anyio.fail_after(2):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096:
                        return empty(413)
                    raw.extend(chunk)
            payload = payload_type.model_validate_json(raw)
        except (ClientDisconnect, TimeoutError, ValueError):
            return empty(422)

        try:
            result = await anyio.to_thread.run_sync(lambda: operation(bearer=bearer, **payload.model_dump()))
        except ClientSessionUnauthenticated:
            return empty(401)
        except ClientSessionAuthorityRejected:
            return empty(403)
        except ValueError:
            return empty(409)
        except Exception:
            # SQL errors can contain protected parameters.
            return empty(503)
        if returns_scope:
            if not isinstance(result, str) or not result.startswith("ctx_") or len(result) != 68:
                return empty(503)
            return JSONResponse(
                content={"scope_ref": result}, status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        return empty(204)

    if owner_scope_registry is not None:
        def enroll(*, bearer, message_id):
            with reader._sessions.begin() as session:
                require_context_database(session, reader_role=reader._role, require_pg_consent=True)
                return owner_scope_registry.enroll(
                    session, bearer=bearer, message_id=message_id,
                    capture_start=capture_start,
                )

        @router.post("/scope/enroll")
        async def enroll_scope(request: Request):
            return await decide(request, ScopeEnrollment, enroll, returns_scope=True)

    @router.post("/grant", status_code=204)
    async def grant(request: Request):
        return await decide(request, GrantDecision, writer.grant_current)

    @router.post("/revoke", status_code=204)
    async def revoke(request: Request):
        return await decide(request, RevokeDecision, writer.revoke_current)

    app.include_router(router)
    return True
