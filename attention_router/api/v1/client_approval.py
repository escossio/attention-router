"""Authenticated Native App Approval V1A HTTP boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_approval import (
    ClientApprovalAuthorityRejected,
    ClientApprovalConflict,
    ClientApprovalDisabled,
    ClientApprovalExpired,
    ClientApprovalNotFound,
    ClientApprovalService,
    ClientApprovalView as ServiceApprovalView,
)
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionDisabled,
    ClientSessionUnauthenticated,
    ClientSessionUnavailable,
)


class ClientApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=64)
    state: Literal[
        "PENDING_HUMAN_APPROVAL",
        "APPROVED",
        "DENIED",
        "EXPIRED",
        "CONSUMED",
        "REVOKED",
    ]
    capability: str = Field(min_length=1, max_length=120)
    operation: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=180)
    preview: str = Field(min_length=1, max_length=4000)
    issued_at: datetime
    expires_at: datetime
class ClientApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"]
    approvals: list[ClientApprovalView] = Field(max_length=50)


class ClientApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "DENY"]


ClientApprovalErrorCode = Literal[
    "CLIENT_APPROVAL_DISABLED",
    "CLIENT_APPROVAL_NOT_FOUND",
    "CLIENT_APPROVAL_AUTHORITY_REJECTED",
    "CLIENT_APPROVAL_CONFLICT",
    "CLIENT_APPROVAL_EXPIRED",
    "CLIENT_APPROVAL_UNAVAILABLE",
    "CLIENT_SESSION_UNAUTHENTICATED",
    "CLIENT_SESSION_AUTHORITY_REJECTED",
    "CLIENT_SESSION_UNAVAILABLE",
]


class ClientApprovalErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ClientApprovalErrorCode


_BEARER = HTTPBearer(
    scheme_name="ClientSession",
    bearerFormat="opaque-client-session-v03c",
    description=(
        "Short-lived opaque client-session credential scoped server-side to "
        "Human Identity, device and active tenant."
    ),
    auto_error=False,
)
_ERROR_STATUS = {
    ClientApprovalDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientApprovalNotFound: status.HTTP_404_NOT_FOUND,
    ClientApprovalAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientApprovalConflict: status.HTTP_409_CONFLICT,
    ClientApprovalExpired: status.HTTP_410_GONE,
    ClientSessionUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientSessionAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientSessionDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientSessionUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    code = getattr(error, "code", "CLIENT_APPROVAL_UNAVAILABLE")
    return JSONResponse(
        status_code=_ERROR_STATUS.get(type(error), status.HTTP_503_SERVICE_UNAVAILABLE),
        content={"code": code},
    )


def _view(result: ServiceApprovalView) -> ClientApprovalView:
    return ClientApprovalView(
        approval_id=result.approval_id,
        state=result.state,
        capability=result.capability,
        operation=result.operation,
        target=result.target,
        preview=result.preview,
        issued_at=result.issued_at,
        expires_at=result.expires_at,
    )


def build_client_approval_router(
    *,
    get_session,
    service: ClientApprovalService,
) -> APIRouter:
    router = APIRouter(tags=["client-approval"])

    def token(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str | None:
        return credentials.credentials if credentials is not None else None
    @router.get(
        "/api/v1/client/approvals/pending",
        response_model=ClientApprovalListResponse,
        responses={
            401: {"model": ClientApprovalErrorResponse},
            403: {"model": ClientApprovalErrorResponse},
            503: {"model": ClientApprovalErrorResponse},
        },
        operation_id="listPendingClientApprovals",
    )
    def list_pending(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientApprovalListResponse | JSONResponse:
        try:
            items = service.list_pending(
                session,
                session_token=token(credentials),
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return ClientApprovalListResponse(
            contract_version="1",
            approvals=[_view(item) for item in items],
        )

    @router.get(
        "/api/v1/client/approvals/{approval_id}",
        response_model=ClientApprovalView,
        responses={
            401: {"model": ClientApprovalErrorResponse},
            403: {"model": ClientApprovalErrorResponse},
            404: {"model": ClientApprovalErrorResponse},
            410: {"model": ClientApprovalErrorResponse},
            503: {"model": ClientApprovalErrorResponse},
        },
        operation_id="getClientApproval",
    )
    def get_approval(
        approval_id: Annotated[str, Path(min_length=1, max_length=64)],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientApprovalView | JSONResponse:
        try:
            result = service.get(
                session,
                session_token=token(credentials),
                approval_id=approval_id,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _view(result)

    @router.post(
        "/api/v1/client/approvals/{approval_id}/decision",
        response_model=ClientApprovalView,
        responses={
            401: {"model": ClientApprovalErrorResponse},
            403: {"model": ClientApprovalErrorResponse},
            404: {"model": ClientApprovalErrorResponse},
            409: {"model": ClientApprovalErrorResponse},
            410: {"model": ClientApprovalErrorResponse},
            503: {"model": ClientApprovalErrorResponse},
        },
        operation_id="decideClientApproval",
    )
    def decide(
        approval_id: Annotated[str, Path(min_length=1, max_length=64)],
        payload: ClientApprovalDecisionRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientApprovalView | JSONResponse:
        try:
            with session.begin_nested():
                result = service.decide(
                    session,
                    session_token=token(credentials),
                    approval_id=approval_id,
                    decision=payload.decision,
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _view(result)

    return router


__all__ = [
    "ClientApprovalDecisionRequest",
    "ClientApprovalErrorResponse",
    "ClientApprovalListResponse",
    "ClientApprovalView",
    "build_client_approval_router",
]
