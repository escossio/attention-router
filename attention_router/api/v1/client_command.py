"""Authenticated Andy Client Command Channel V1 HTTP boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_command import (
    ClientCommandAuthorityRejected,
    ClientCommandConflict,
    ClientCommandDisabled,
    ClientCommandInvalid,
    ClientCommandVoiceDisabled,
    ClientCommandVoiceInvalid,
    ClientCommandVoiceUnavailable,
    ClientCommandService,
    ClientCommandView as ServiceCommandView,
)
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionDisabled,
    ClientSessionUnauthenticated,
    ClientSessionUnavailable,
)


class ClientCommandSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    text: str = Field(min_length=1, max_length=4000)


class ClientCommandView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=64)
    client_request_id: str = Field(min_length=1, max_length=80)
    modality: Literal["TEXT", "VOICE"]
    input_text: str = Field(min_length=1, max_length=4000)
    state: Literal[
        "RECEIVED",
        "COMPLETED",
        "CLARIFICATION_REQUIRED",
        "GENERAL_TASK_PENDING",
        "FAILED",
    ]
    normalized_action: str | None = Field(default=None, max_length=80)
    response_text: str | None = Field(default=None, max_length=4000)
    error_code: str | None = Field(default=None, max_length=120)
    created_at: datetime
    processed_at: datetime | None


class ClientCommandListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"]
    commands: list[ClientCommandView] = Field(max_length=100)


ClientCommandErrorCode = Literal[
    "CLIENT_COMMAND_DISABLED",
    "CLIENT_COMMAND_AUTHORITY_REJECTED",
    "CLIENT_COMMAND_CONFLICT",
    "CLIENT_COMMAND_INVALID",
    "CLIENT_COMMAND_VOICE_DISABLED",
    "CLIENT_COMMAND_VOICE_INVALID",
    "CLIENT_COMMAND_VOICE_UNAVAILABLE",
    "CLIENT_COMMAND_UNAVAILABLE",
    "CLIENT_SESSION_UNAUTHENTICATED",
    "CLIENT_SESSION_AUTHORITY_REJECTED",
    "CLIENT_SESSION_UNAVAILABLE",
]


class ClientCommandErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ClientCommandErrorCode


_BEARER = HTTPBearer(
    scheme_name="ClientSession",
    bearerFormat="opaque-client-session-v03c",
    description=(
        "Short-lived opaque Client Session scoped server-side to Human Identity, "
        "device and active tenant."
    ),
    auto_error=False,
)

_ERROR_STATUS = {
    ClientCommandDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientCommandAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientCommandConflict: status.HTTP_409_CONFLICT,
    ClientCommandInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ClientCommandVoiceDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientCommandVoiceInvalid: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ClientCommandVoiceUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientSessionUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientSessionAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientSessionDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientSessionUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    code = getattr(error, "code", "CLIENT_COMMAND_UNAVAILABLE")
    return JSONResponse(
        status_code=_ERROR_STATUS.get(
            type(error),
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
        content={"code": code},
    )


def _view(result: ServiceCommandView) -> ClientCommandView:
    return ClientCommandView(
        command_id=result.command_id,
        client_request_id=result.client_request_id,
        modality=result.modality,
        input_text=result.input_text,
        state=result.state,
        normalized_action=result.normalized_action,
        response_text=result.response_text,
        error_code=result.error_code,
        created_at=result.created_at,
        processed_at=result.processed_at,
    )


def build_client_command_router(
    *,
    get_session,
    service: ClientCommandService,
) -> APIRouter:
    router = APIRouter(tags=["client-command"])

    def token(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str | None:
        return credentials.credentials if credentials is not None else None

    @router.post(
        "/api/v1/client/commands",
        response_model=ClientCommandView,
        responses={
            401: {"model": ClientCommandErrorResponse},
            403: {"model": ClientCommandErrorResponse},
            409: {"model": ClientCommandErrorResponse},
            422: {"model": ClientCommandErrorResponse},
            503: {"model": ClientCommandErrorResponse},
        },
        operation_id="submitClientCommand",
    )
    def submit_command(
        payload: ClientCommandSubmitRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientCommandView | JSONResponse:
        try:
            result = service.submit_text(
                session,
                session_token=token(credentials),
                client_request_id=payload.client_request_id,
                text=payload.text,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _view(result)

    @router.post(
        "/api/v1/client/commands/voice",
        response_model=ClientCommandView,
        responses={
            401: {"model": ClientCommandErrorResponse},
            403: {"model": ClientCommandErrorResponse},
            409: {"model": ClientCommandErrorResponse},
            422: {"model": ClientCommandErrorResponse},
            503: {"model": ClientCommandErrorResponse},
        },
        operation_id="submitClientVoiceCommand",
    )
    def submit_voice_command(
        audio: Annotated[
            bytes,
            Body(media_type="audio/mp4", max_length=5 * 1024 * 1024),
        ],
        client_request_id: Annotated[
            str,
            Header(
                alias="X-Client-Request-Id",
                min_length=1,
                max_length=80,
                pattern=r"^[A-Za-z0-9_.:-]+$",
            ),
        ],
        content_type: Annotated[
            str,
            Header(alias="Content-Type", min_length=1, max_length=80),
        ],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientCommandView | JSONResponse:
        try:
            result = service.submit_voice(
                session,
                session_token=token(credentials),
                client_request_id=client_request_id,
                audio=audio,
                mime_type=content_type,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _view(result)

    @router.get(
        "/api/v1/client/commands",
        response_model=ClientCommandListResponse,
        responses={
            401: {"model": ClientCommandErrorResponse},
            403: {"model": ClientCommandErrorResponse},
            503: {"model": ClientCommandErrorResponse},
        },
        operation_id="listClientCommands",
    )
    def list_commands(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        session: Session = Depends(get_session),
    ) -> ClientCommandListResponse | JSONResponse:
        try:
            items = service.list_recent(
                session,
                session_token=token(credentials),
                limit=limit,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return ClientCommandListResponse(
            contract_version="1",
            commands=[_view(item) for item in items],
        )

    return router


__all__ = [
    "ClientCommandErrorResponse",
    "ClientCommandListResponse",
    "ClientCommandSubmitRequest",
    "ClientCommandView",
    "build_client_command_router",
]
