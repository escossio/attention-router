"""HTTP boundary for V0.3C client-session authority."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.api.v1.client_bootstrap import ClientDeviceView, ClientTenantMembershipView
from attention_router.application.client_session import (
    ClientSessionActiveTenantRequired, ClientSessionAuthorityRejected,
    ClientSessionChallengeConflict, ClientSessionChallengeConsumed,
    ClientSessionChallengeExpired, ClientSessionChallengeNotFound,
    ClientSessionDeviceRejected, ClientSessionDisabled, ClientSessionService,
    ClientSessionSignatureInvalid, ClientSessionTenantForbidden,
    ClientSessionUnauthenticated, ClientSessionUnavailable,
)


class ClientSessionChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key_spki_b64url: str = Field(
        min_length=80, max_length=2048, pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Unpadded base64url DER SubjectPublicKeyInfo for the already enrolled "
            "P-256 device key. The server derives the fingerprint and resolves "
            "device authority; this value is not authority by itself."
        ),
    )
    requested_tenant_id: str | None = Field(
        default=None, min_length=1, max_length=64,
        description=(
            "Optional tenant selection assertion. It grants no authority and must "
            "resolve to a current ACTIVE membership."
        ),
    )


class ClientSessionChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_challenge_id: str = Field(pattern=r"^csc_[A-Za-z0-9_-]{20,}$")
    challenge_b64url: str = Field(min_length=32, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
    expires_at: datetime


class ClientSessionCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_signature_b64url: str = Field(
        min_length=64, max_length=512, pattern=r"^[A-Za-z0-9_-]+$", repr=False,
        description="ECDSA/SHA-256 signature over the exact server session challenge bytes.",
    )


class ClientSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^csn_[A-Za-z0-9_-]{20,}$")
    session_token: str = Field(
        min_length=47, max_length=47, pattern=r"^cst_[A-Za-z0-9_-]{43}$", repr=False,
        description=(
            "Sensitive short-lived opaque bearer credential returned once. "
            "Never persist raw server-side or log."
        ),
    )
    token_type: Literal["Bearer"]
    expires_at: datetime
    human_identity_id: str = Field(pattern=r"^hid_[A-Za-z0-9_-]{20,}$")
    device_id: str = Field(pattern=r"^cdev_[A-Za-z0-9_-]{20,}$")
    tenant_id: str = Field(min_length=1, max_length=64)


class ClientSessionEstablishedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["CLIENT_SESSION_ESTABLISHED"]
    session: ClientSessionView


class AuthenticatedClientBootstrapSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["1"]
    human_identity_id: str = Field(pattern=r"^hid_[A-Za-z0-9_-]{20,}$")
    active_tenant_id: str = Field(min_length=1, max_length=64)
    memberships: list[ClientTenantMembershipView] = Field(min_length=1, max_length=100)
    device: ClientDeviceView
    session_expires_at: datetime
    server_time: datetime


ClientSessionErrorCode = Literal[
    "CLIENT_SESSION_DEVICE_REJECTED", "CLIENT_SESSION_CHALLENGE_NOT_FOUND",
    "CLIENT_SESSION_CHALLENGE_EXPIRED", "CLIENT_SESSION_CHALLENGE_CONSUMED",
    "CLIENT_SESSION_SIGNATURE_INVALID", "CLIENT_SESSION_ACTIVE_TENANT_REQUIRED",
    "CLIENT_SESSION_TENANT_FORBIDDEN", "CLIENT_SESSION_UNAUTHENTICATED",
    "CLIENT_SESSION_AUTHORITY_REJECTED", "CLIENT_SESSION_UNAVAILABLE",
]


class ClientSessionErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ClientSessionErrorCode


_BEARER = HTTPBearer(
    scheme_name="ClientSession", bearerFormat="opaque-client-session-v03c",
    description=(
        "Short-lived opaque client-session credential scoped server-side to "
        "Human Identity, device and active tenant."
    ),
    auto_error=False,
)

_ERROR_STATUS = {
    ClientSessionDeviceRejected: status.HTTP_401_UNAUTHORIZED,
    ClientSessionChallengeNotFound: status.HTTP_404_NOT_FOUND,
    ClientSessionChallengeExpired: status.HTTP_410_GONE,
    ClientSessionChallengeConsumed: status.HTTP_409_CONFLICT,
    ClientSessionChallengeConflict: status.HTTP_409_CONFLICT,
    ClientSessionSignatureInvalid: status.HTTP_401_UNAUTHORIZED,
    ClientSessionActiveTenantRequired: status.HTTP_409_CONFLICT,
    ClientSessionTenantForbidden: status.HTTP_401_UNAUTHORIZED,
    ClientSessionUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientSessionAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientSessionDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientSessionUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(status_code=_ERROR_STATUS[type(error)], content={"code": error.code})


def build_client_session_router(*, get_session, service: ClientSessionService) -> APIRouter:
    router = APIRouter(tags=["client-session"])

    @router.post(
        "/api/v1/session/device/challenges",
        response_model=ClientSessionChallengeResponse,
        status_code=status.HTTP_201_CREATED,
        responses={400: {"model": ClientSessionErrorResponse}, 401: {"model": ClientSessionErrorResponse},
                   409: {"model": ClientSessionErrorResponse}, 503: {"model": ClientSessionErrorResponse}},
        operation_id="createClientSessionChallenge", openapi_extra={"security": []},
    )
    def start_session(payload: ClientSessionChallengeRequest,
                      session: Session = Depends(get_session)) -> ClientSessionChallengeResponse | JSONResponse:
        try:
            with session.begin_nested():
                result = service.start_session(
                    session, public_key_spki_b64url=payload.public_key_spki_b64url,
                    requested_tenant_id=payload.requested_tenant_id,
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return ClientSessionChallengeResponse(
            session_challenge_id=result.session_challenge_id,
            challenge_b64url=result.challenge_b64url, expires_at=result.expires_at,
        )

    @router.post(
        "/api/v1/session/device/challenges/{session_challenge_id}/complete",
        response_model=ClientSessionEstablishedResponse,
        responses={400: {"model": ClientSessionErrorResponse}, 401: {"model": ClientSessionErrorResponse},
                   404: {"model": ClientSessionErrorResponse}, 409: {"model": ClientSessionErrorResponse},
                   410: {"model": ClientSessionErrorResponse}, 503: {"model": ClientSessionErrorResponse}},
        operation_id="completeClientSessionChallenge", openapi_extra={"security": []},
    )
    def complete_session(
        session_challenge_id: Annotated[str, Path(pattern=r"^csc_[A-Za-z0-9_-]{20,}$")],
        payload: ClientSessionCompleteRequest,
        session: Session = Depends(get_session),
    ) -> ClientSessionEstablishedResponse | JSONResponse:
        try:
            with session.begin_nested():
                result = service.complete_session(
                    session, session_challenge_id=session_challenge_id,
                    device_signature_b64url=payload.device_signature_b64url,
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return ClientSessionEstablishedResponse(
            status=result.status,
            session=ClientSessionView(
                session_id=result.session_id, session_token=result.session_token,
                token_type=result.token_type, expires_at=result.expires_at,
                human_identity_id=result.human_identity_id, device_id=result.device_id,
                tenant_id=result.tenant_id,
            ),
        )

    @router.get(
        "/api/v1/client/bootstrap",
        response_model=AuthenticatedClientBootstrapSnapshot,
        responses={401: {"model": ClientSessionErrorResponse}, 403: {"model": ClientSessionErrorResponse},
                   409: {"model": ClientSessionErrorResponse}, 503: {"model": ClientSessionErrorResponse}},
        operation_id="getAuthenticatedClientBootstrap",
    )
    def authenticated_bootstrap(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
        session: Session = Depends(get_session),
    ) -> AuthenticatedClientBootstrapSnapshot | JSONResponse:
        token = credentials.credentials if credentials is not None else None
        try:
            result = service.authenticated_bootstrap(session, session_token=token)
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return AuthenticatedClientBootstrapSnapshot(
            contract_version=result.contract_version, human_identity_id=result.human_identity_id,
            active_tenant_id=result.active_tenant_id,
            memberships=[
                ClientTenantMembershipView(
                    membership_id=item.membership_id, tenant_id=item.tenant_id,
                    role=item.role.value, status=item.status.value,
                ) for item in result.memberships
            ],
            device=ClientDeviceView(
                device_id=result.device.device_id,
                public_key_fingerprint=result.device.public_key_fingerprint,
                canonical_name=result.device.canonical_name,
                platform=result.device.platform.value,
                roles=[role.value for role in result.device.roles],
                status=result.device.status.value,
            ),
            session_expires_at=result.session_expires_at, server_time=result.server_time,
        )

    return router
