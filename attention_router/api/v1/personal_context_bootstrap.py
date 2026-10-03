"""Authenticated API for owner-controlled Personal Context bootstrap."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionError
from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
)
from attention_router.application.personal_context_bootstrap_product import (
    PersonalContextBootstrapProductBudgetInvalid,
    PersonalContextBootstrapProductDisabled,
    PersonalContextBootstrapProductError,
    PersonalContextBootstrapProductRunNotFound,
    PersonalContextBootstrapProductSelectionInvalid,
    PersonalContextBootstrapProductService,
    PersonalContextBootstrapProductTenantForbidden,
)
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.integrations.whatsapp_history import WhatsAppHistoryError


class PersonalContextBootstrapCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent_ref: str = Field(min_length=1, max_length=240)
    chat_keys: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=5000,
    )
    processing_budget: dict[str, int] | None = None


class PersonalContextBootstrapRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"]
    run_id: str
    source_kind: Literal["WHATSAPP_TEXT"]
    state: Literal[
        "CREATED",
        "QUEUED",
        "RUNNING",
        "PAUSED",
        "COMPLETED",
        "CANCELLED",
        "FAILED",
    ]
    requested_control: Literal["NONE", "PAUSE", "CANCEL"]
    source_selection: dict[str, Any]
    processing_budget: dict[str, Any]
    progress: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_at: datetime | None = None
    failure_summary: str | None = Field(default=None, max_length=500)


class PersonalContextBootstrapErrorResponse(BaseModel):
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


def _product_status(error: PersonalContextBootstrapProductError) -> int:
    if isinstance(error, PersonalContextBootstrapProductTenantForbidden):
        return status.HTTP_403_FORBIDDEN
    if isinstance(error, PersonalContextBootstrapProductRunNotFound):
        return status.HTTP_404_NOT_FOUND
    if isinstance(
        error,
        (
            PersonalContextBootstrapProductSelectionInvalid,
            PersonalContextBootstrapProductBudgetInvalid,
        ),
    ):
        return status.HTTP_400_BAD_REQUEST
    if isinstance(error, PersonalContextBootstrapProductDisabled):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _lifecycle_status(error: PersonalContextBootstrapError) -> int:
    code = str(error)
    if code == "BOOTSTRAP_RUN_NOT_FOUND":
        return status.HTTP_404_NOT_FOUND
    if code in {
        "BOOTSTRAP_OWNER_MEMBERSHIP_REQUIRED",
        "BOOTSTRAP_REPRESENTED_OWNER_AMBIGUOUS",
        "BOOTSTRAP_REPRESENTED_OWNER_UNRESOLVED",
        "BOOTSTRAP_TENANT_UNAVAILABLE",
    }:
        return status.HTTP_403_FORBIDDEN
    if code in {
        "BOOTSTRAP_RUN_NOT_QUEUEABLE",
        "BOOTSTRAP_RUN_NOT_RESUMABLE",
        "BOOTSTRAP_RUN_NOT_QUEUED",
        "BOOTSTRAP_RUN_STATE_INVALID",
    }:
        return status.HTTP_409_CONFLICT
    return status.HTTP_400_BAD_REQUEST


def _view(row: PersonalContextBootstrapRunRow) -> PersonalContextBootstrapRunResponse:
    return PersonalContextBootstrapRunResponse(
        contract_version="1",
        run_id=row.id,
        source_kind=row.source_kind,
        state=row.state,
        requested_control=row.requested_control,
        source_selection=dict(row.source_selection or {}),
        processing_budget=dict(row.processing_budget or {}),
        progress=dict(row.progress or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        paused_at=row.paused_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        failed_at=row.failed_at,
        failure_summary=row.failure_summary,
    )


def build_personal_context_bootstrap_router(
    *,
    get_session,
    service: PersonalContextBootstrapProductService,
) -> APIRouter:
    router = APIRouter(tags=["personal-context-bootstrap"])

    def _token(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str | None:
        return credentials.credentials if credentials else None

    def _error(error: Exception) -> JSONResponse:
        if isinstance(error, ClientSessionError):
            code = error.code
            http_status = _session_status(error)
        elif isinstance(error, PersonalContextBootstrapProductError):
            code = error.code
            http_status = _product_status(error)
        elif isinstance(error, PersonalContextBootstrapError):
            http_status = _lifecycle_status(error)
            code = {
                status.HTTP_400_BAD_REQUEST: (
                    "PERSONAL_CONTEXT_BOOTSTRAP_INVALID"
                ),
                status.HTTP_403_FORBIDDEN: (
                    "PERSONAL_CONTEXT_BOOTSTRAP_FORBIDDEN"
                ),
                status.HTTP_404_NOT_FOUND: (
                    "PERSONAL_CONTEXT_BOOTSTRAP_RUN_NOT_FOUND"
                ),
                status.HTTP_409_CONFLICT: (
                    "PERSONAL_CONTEXT_BOOTSTRAP_CONFLICT"
                ),
            }.get(
                http_status,
                "PERSONAL_CONTEXT_BOOTSTRAP_INVALID",
            )
        elif isinstance(error, WhatsAppHistoryError):
            code = error.code
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            raise error
        return JSONResponse(
            status_code=http_status,
            content={"code": code},
        )

    @router.post(
        "/api/v1/personal-context/bootstrap",
        response_model=PersonalContextBootstrapRunResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": PersonalContextBootstrapErrorResponse},
            401: {"model": PersonalContextBootstrapErrorResponse},
            403: {"model": PersonalContextBootstrapErrorResponse},
            503: {"model": PersonalContextBootstrapErrorResponse},
        },
        operation_id="createPersonalContextBootstrap",
    )
    def create(
        payload: PersonalContextBootstrapCreateRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        try:
            with session.begin_nested():
                row = service.create_and_queue(
                    session,
                    session_token=_token(credentials),
                    consent_ref=payload.consent_ref,
                    chat_keys=payload.chat_keys,
                    processing_budget=payload.processing_budget,
                )
            return _view(row)
        except (
            ClientSessionError,
            PersonalContextBootstrapProductError,
            PersonalContextBootstrapError,
            WhatsAppHistoryError,
        ) as error:
            return _error(error)

    @router.get(
        "/api/v1/personal-context/bootstrap/{run_id}",
        response_model=PersonalContextBootstrapRunResponse,
        responses={
            401: {"model": PersonalContextBootstrapErrorResponse},
            403: {"model": PersonalContextBootstrapErrorResponse},
            404: {"model": PersonalContextBootstrapErrorResponse},
            503: {"model": PersonalContextBootstrapErrorResponse},
        },
        operation_id="getPersonalContextBootstrap",
    )
    def get_run(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        try:
            return _view(
                service.status(
                    session,
                    session_token=_token(credentials),
                    run_id=run_id,
                )
            )
        except (
            ClientSessionError,
            PersonalContextBootstrapProductError,
            PersonalContextBootstrapError,
        ) as error:
            return _error(error)

    def _control(
        action: str,
        *,
        session: Session,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        try:
            handler = getattr(service, action)
            with session.begin_nested():
                row = handler(
                    session,
                    session_token=session_token,
                    run_id=run_id,
                )
            return _view(row)
        except (
            ClientSessionError,
            PersonalContextBootstrapProductError,
            PersonalContextBootstrapError,
        ) as error:
            return _error(error)

    @router.post(
        "/api/v1/personal-context/bootstrap/{run_id}/pause",
        response_model=PersonalContextBootstrapRunResponse,
        operation_id="pausePersonalContextBootstrap",
    )
    def pause(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        return _control(
            "pause",
            session=session,
            session_token=_token(credentials),
            run_id=run_id,
        )

    @router.post(
        "/api/v1/personal-context/bootstrap/{run_id}/resume",
        response_model=PersonalContextBootstrapRunResponse,
        operation_id="resumePersonalContextBootstrap",
    )
    def resume(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        return _control(
            "resume",
            session=session,
            session_token=_token(credentials),
            run_id=run_id,
        )

    @router.post(
        "/api/v1/personal-context/bootstrap/{run_id}/cancel",
        response_model=PersonalContextBootstrapRunResponse,
        operation_id="cancelPersonalContextBootstrap",
    )
    def cancel(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> PersonalContextBootstrapRunResponse | JSONResponse:
        return _control(
            "cancel",
            session=session,
            session_token=_token(credentials),
            run_id=run_id,
        )

    return router


__all__ = [
    "PersonalContextBootstrapCreateRequest",
    "PersonalContextBootstrapErrorResponse",
    "PersonalContextBootstrapRunResponse",
    "build_personal_context_bootstrap_router",
]
