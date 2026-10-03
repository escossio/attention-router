"""Authenticated client profile contract."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_profile import (
    ClientProfileAuthorityRejected,
    ClientProfileDisabled,
    ClientProfileService,
)
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionDisabled,
    ClientSessionUnauthenticated,
    ClientSessionUnavailable,
)


class ClientProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1"
    assistant_reference_name: str | None = Field(default=None, max_length=160)
    updated_at: datetime | None = None


class ClientProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_reference_name: str | None = Field(max_length=160)


class ClientProfileErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str


_BEARER = HTTPBearer(
    scheme_name="ClientSession",
    bearerFormat="opaque-client-session-v03c",
    auto_error=False,
)
_ERROR_STATUS = {
    ClientProfileDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientProfileAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientSessionUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientSessionAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientSessionDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientSessionUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(type(error), status.HTTP_503_SERVICE_UNAVAILABLE),
        content={"code": getattr(error, "code", "CLIENT_PROFILE_UNAVAILABLE")},
    )


def _view(result) -> ClientProfileView:
    return ClientProfileView(
        assistant_reference_name=result.assistant_reference_name,
        updated_at=result.updated_at,
    )


def build_client_profile_router(*, get_session, service: ClientProfileService) -> APIRouter:
    router = APIRouter(tags=["client-profile"])

    def token(credentials: HTTPAuthorizationCredentials | None) -> str | None:
        return credentials.credentials if credentials is not None else None

    @router.get(
        "/api/v1/client/profile",
        response_model=ClientProfileView,
        operation_id="getClientProfile",
    )
    def get_profile(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
        session: Session = Depends(get_session),
    ) -> ClientProfileView | JSONResponse:
        try:
            return _view(service.get(session, session_token=token(credentials)))
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)

    @router.patch(
        "/api/v1/client/profile",
        response_model=ClientProfileView,
        operation_id="updateClientProfile",
    )
    def update_profile(
        payload: ClientProfilePatch,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
        session: Session = Depends(get_session),
    ) -> ClientProfileView | JSONResponse:
        try:
            return _view(
                service.update(
                    session,
                    session_token=token(credentials),
                    assistant_reference_name=payload.assistant_reference_name,
                )
            )
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"code": "CLIENT_PROFILE_REFERENCE_NAME_INVALID"},
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)

    return router


__all__ = [
    "ClientProfileErrorResponse",
    "ClientProfilePatch",
    "ClientProfileView",
    "build_client_profile_router",
]
