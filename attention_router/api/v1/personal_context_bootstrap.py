"""Authenticated Client API surface for Semantic Bootstrap."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionError
from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
)
from attention_router.application.personal_context_bootstrap_product import (
    BootstrapProductView,
    PersonalContextBootstrapProductService,
)


ChatKey = Annotated[str, Field(min_length=1, max_length=240)]


class BootstrapStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    chat_keys: list[ChatKey] = Field(default_factory=list, max_length=100)


class BootstrapControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["PAUSE", "CANCEL", "RESUME"]


class BootstrapRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["1"] = "1"
    run_id: str
    state: str
    source_kind: Literal["WHATSAPP_TEXT"]
    source_account: str
    progress: dict
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None
    failure_summary: str | None = None


class BootstrapRunList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["1"] = "1"
    runs: list[BootstrapRunView]


class BootstrapErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=100)


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


def _bootstrap_status(error: PersonalContextBootstrapError) -> int:
    code = str(error)
    if code == "BOOTSTRAP_RUN_NOT_FOUND":
        return status.HTTP_404_NOT_FOUND
    if code in {
        "BOOTSTRAP_OWNER_MEMBERSHIP_REQUIRED",
        "BOOTSTRAP_REPRESENTED_OWNER_UNRESOLVED",
    }:
        return status.HTTP_403_FORBIDDEN
    if code in {
        "BOOTSTRAP_REPRESENTED_OWNER_AMBIGUOUS",
        "BOOTSTRAP_RUN_NOT_QUEUEABLE",
        "BOOTSTRAP_RUN_NOT_RESUMABLE",
        "BOOTSTRAP_RUN_STATE_INVALID",
    }:
        return status.HTTP_409_CONFLICT
    if code in {
        "BOOTSTRAP_CLIENT_REQUEST_ID_INVALID",
        "BOOTSTRAP_CHAT_SELECTION_INVALID",
        "BOOTSTRAP_CONTROL_UNSUPPORTED",
        "BOOTSTRAP_SOURCE_KIND_UNSUPPORTED",
        "BOOTSTRAP_SOURCE_SELECTION_FIELD_UNSUPPORTED",
        "BOOTSTRAP_SOURCE_SELECTION_INVALID",
        "BOOTSTRAP_PROCESSING_BUDGET_FIELD_UNSUPPORTED",
        "BOOTSTRAP_PROCESSING_BUDGET_INVALID",
    }:
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _view(value: BootstrapProductView) -> BootstrapRunView:
    return BootstrapRunView(
        run_id=value.run_id,
        state=value.state,
        source_kind=value.source_kind,
        source_account=value.source_account,
        progress=value.progress,
        created_at=value.created_at,
        updated_at=value.updated_at,
        started_at=value.started_at,
        paused_at=value.paused_at,
        completed_at=value.completed_at,
        cancelled_at=value.cancelled_at,
        failed_at=value.failed_at,
        failure_summary=value.failure_summary,
    )


def build_personal_context_bootstrap_router(
    *,
    get_session,
    service: PersonalContextBootstrapProductService,
) -> APIRouter:
    router = APIRouter(tags=["personal-context-bootstrap"])

    def token(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str | None:
        return credentials.credentials if credentials is not None else None

    def error_response(error: Exception) -> JSONResponse:
        if isinstance(error, ClientSessionError):
            code = error.code
            status_code = _session_status(error)
        else:
            code = str(error)
            status_code = _bootstrap_status(error)
        return JSONResponse(status_code=status_code, content={"code": code})

    @router.post(
        "/api/v1/client/personal-context/bootstrap",
        response_model=BootstrapRunView,
        responses={
            401: {"model": BootstrapErrorResponse},
            403: {"model": BootstrapErrorResponse},
            409: {"model": BootstrapErrorResponse},
            422: {"model": BootstrapErrorResponse},
            503: {"model": BootstrapErrorResponse},
        },
        operation_id="startPersonalContextBootstrap",
    )
    def start(
        payload: BootstrapStartRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> BootstrapRunView | JSONResponse:
        try:
            with session.begin_nested():
                result = service.start(
                    session,
                    session_token=token(credentials),
                    client_request_id=payload.client_request_id,
                    chat_keys=tuple(payload.chat_keys),
                )
            return _view(result)
        except (ClientSessionError, PersonalContextBootstrapError) as error:
            return error_response(error)

    @router.get(
        "/api/v1/client/personal-context/bootstrap",
        response_model=BootstrapRunList,
        responses={
            401: {"model": BootstrapErrorResponse},
            403: {"model": BootstrapErrorResponse},
            503: {"model": BootstrapErrorResponse},
        },
        operation_id="listPersonalContextBootstrapRuns",
    )
    def list_runs(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        session: Session = Depends(get_session),
    ) -> BootstrapRunList | JSONResponse:
        try:
            values = service.list_recent(
                session,
                session_token=token(credentials),
                limit=limit,
            )
            return BootstrapRunList(runs=[_view(value) for value in values])
        except (ClientSessionError, PersonalContextBootstrapError) as error:
            return error_response(error)

    @router.get(
        "/api/v1/client/personal-context/bootstrap/{run_id}",
        response_model=BootstrapRunView,
        responses={
            401: {"model": BootstrapErrorResponse},
            403: {"model": BootstrapErrorResponse},
            404: {"model": BootstrapErrorResponse},
            503: {"model": BootstrapErrorResponse},
        },
        operation_id="getPersonalContextBootstrapRun",
    )
    def get_run(
        run_id: str,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> BootstrapRunView | JSONResponse:
        try:
            return _view(
                service.status(
                    session,
                    session_token=token(credentials),
                    run_id=run_id,
                )
            )
        except (ClientSessionError, PersonalContextBootstrapError) as error:
            return error_response(error)

    @router.post(
        "/api/v1/client/personal-context/bootstrap/{run_id}/control",
        response_model=BootstrapRunView,
        responses={
            401: {"model": BootstrapErrorResponse},
            403: {"model": BootstrapErrorResponse},
            404: {"model": BootstrapErrorResponse},
            409: {"model": BootstrapErrorResponse},
            422: {"model": BootstrapErrorResponse},
            503: {"model": BootstrapErrorResponse},
        },
        operation_id="controlPersonalContextBootstrapRun",
    )
    def control(
        run_id: str,
        payload: BootstrapControlRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> BootstrapRunView | JSONResponse:
        try:
            with session.begin_nested():
                result = service.control(
                    session,
                    session_token=token(credentials),
                    run_id=run_id,
                    action=payload.action,
                )
            return _view(result)
        except (ClientSessionError, PersonalContextBootstrapError) as error:
            return error_response(error)

    return router


__all__ = [
    "BootstrapControlRequest",
    "BootstrapErrorResponse",
    "BootstrapRunList",
    "BootstrapRunView",
    "BootstrapStartRequest",
    "build_personal_context_bootstrap_router",
]
