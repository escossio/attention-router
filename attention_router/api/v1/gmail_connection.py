"""Authenticated product surface for connecting/disconnecting Gmail."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionError
from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailAuthorizationRejected,
    GmailConnectionConflict,
    GmailConnectionDisabled,
    GmailConnectionError,
    GmailConnectionService,
    GmailProviderUnavailable,
    GmailRefreshTokenRequired,
)


class GmailConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_code: str = Field(
        min_length=8,
        max_length=8192,
        repr=False,
        description=(
            "One-time Google server authorization code returned by Android "
            "AuthorizationClient offline access. Never persisted or logged."
        ),
    )


class GmailConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["1"]
    provider: Literal["GOOGLE"]
    product: Literal["GMAIL"]
    status: Literal["CONNECTED", "DISCONNECTED"]
    installation_id: str | None = Field(default=None, max_length=64)
    granted_scopes: list[str] = Field(max_length=20)


class GmailConnectionErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=80)


_BEARER = HTTPBearer(
    scheme_name="ClientSession",
    bearerFormat="opaque-client-session-v03c",
    auto_error=False,
)


def _session_status(error: ClientSessionError) -> int:
    if error.code == "CLIENT_SESSION_UNAUTHENTICATED":
        return status.HTTP_401_UNAUTHORIZED
    if error.code in {
        "CLIENT_SESSION_AUTHORITY_REJECTED",
        "CLIENT_SESSION_TENANT_FORBIDDEN",
        "CLIENT_SESSION_DEVICE_REJECTED",
    }:
        return status.HTTP_403_FORBIDDEN
    if error.code == "CLIENT_SESSION_ACTIVE_TENANT_REQUIRED":
        return status.HTTP_409_CONFLICT
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _gmail_status(error: GmailConnectionError) -> int:
    if isinstance(error, GmailAuthorizationRejected):
        return status.HTTP_400_BAD_REQUEST
    if isinstance(error, (GmailRefreshTokenRequired, GmailConnectionConflict)):
        return status.HTTP_409_CONFLICT
    if isinstance(error, (GmailConnectionDisabled, GmailProviderUnavailable)):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _response(view) -> GmailConnectionResponse:
    return GmailConnectionResponse(
        contract_version="1",
        provider="GOOGLE",
        product="GMAIL",
        status=view.status,
        installation_id=view.installation_id,
        granted_scopes=list(view.granted_scopes),
    )


def build_gmail_connection_router(
    *,
    get_session,
    service: GmailConnectionService,
) -> APIRouter:
    router = APIRouter(tags=["gmail-integration"])

    @router.get(
        "/api/v1/integrations/gmail",
        response_model=GmailConnectionResponse,
        responses={
            401: {"model": GmailConnectionErrorResponse},
            403: {"model": GmailConnectionErrorResponse},
            503: {"model": GmailConnectionErrorResponse},
        },
        operation_id="getGmailConnection",
    )
    def get_connection(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> GmailConnectionResponse | JSONResponse:
        token = credentials.credentials if credentials else None
        try:
            return _response(
                service.status(session, session_token=token)
            )
        except ClientSessionError as error:
            return JSONResponse(
                status_code=_session_status(error),
                content={"code": error.code},
            )
        except GmailConnectionError as error:
            return JSONResponse(
                status_code=_gmail_status(error),
                content={"code": error.code},
            )

    @router.post(
        "/api/v1/integrations/gmail",
        response_model=GmailConnectionResponse,
        responses={
            400: {"model": GmailConnectionErrorResponse},
            401: {"model": GmailConnectionErrorResponse},
            403: {"model": GmailConnectionErrorResponse},
            409: {"model": GmailConnectionErrorResponse},
            503: {"model": GmailConnectionErrorResponse},
        },
        operation_id="connectGmail",
    )
    def connect(
        payload: GmailConnectRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> GmailConnectionResponse | JSONResponse:
        token = credentials.credentials if credentials else None
        try:
            with session.begin_nested():
                view = service.connect(
                    session,
                    session_token=token,
                    authorization_code=payload.authorization_code,
                )
            return _response(view)
        except ClientSessionError as error:
            return JSONResponse(
                status_code=_session_status(error),
                content={"code": error.code},
            )
        except GmailConnectionError as error:
            return JSONResponse(
                status_code=_gmail_status(error),
                content={"code": error.code},
            )

    @router.delete(
        "/api/v1/integrations/gmail",
        response_model=GmailConnectionResponse,
        responses={
            401: {"model": GmailConnectionErrorResponse},
            403: {"model": GmailConnectionErrorResponse},
            503: {"model": GmailConnectionErrorResponse},
        },
        operation_id="disconnectGmail",
    )
    def disconnect(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> GmailConnectionResponse | JSONResponse:
        token = credentials.credentials if credentials else None
        try:
            with session.begin_nested():
                view = service.disconnect(
                    session,
                    session_token=token,
                )
            return _response(view)
        except ClientSessionError as error:
            return JSONResponse(
                status_code=_session_status(error),
                content={"code": error.code},
            )
        except GmailConnectionError as error:
            return JSONResponse(
                status_code=_gmail_status(error),
                content={"code": error.code},
            )

    return router


__all__ = [
    "GMAIL_METADATA_SCOPE",
    "GmailConnectRequest",
    "GmailConnectionResponse",
    "build_gmail_connection_router",
]
