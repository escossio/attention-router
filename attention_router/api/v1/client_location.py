"""HTTP boundary for V0.4A current client location snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_location import (
    ClientLocationAuthorityRejected,
    ClientLocationDisabled,
    ClientLocationFuture,
    ClientLocationInvalid,
    ClientLocationNotFound,
    ClientLocationService,
    ClientLocationStale,
    ClientLocationUnauthenticated,
    ClientLocationUnavailable,
)
from attention_router.core.client.location import (
    ClientLocationPrecision,
    CurrentLocationObservation,
)


class ClientLocationWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=10_000)
    captured_at: datetime
    precision: ClientLocationPrecision | None = None


class ClientLocationSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["1"]
    location_snapshot_id: str = Field(pattern=r"^cloc_[A-Za-z0-9_-]{20,}$")
    human_identity_id: str = Field(pattern=r"^hid_[A-Za-z0-9_-]{20,}$")
    device_id: str = Field(pattern=r"^cdev_[A-Za-z0-9_-]{20,}$")
    tenant_id: str = Field(min_length=1, max_length=64)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(gt=0, le=10_000)
    precision: ClientLocationPrecision | None = None
    captured_at: datetime
    received_at: datetime


ClientLocationErrorCode = Literal[
    "CLIENT_LOCATION_DISABLED",
    "CLIENT_LOCATION_INVALID",
    "CLIENT_LOCATION_STALE",
    "CLIENT_LOCATION_FUTURE",
    "CLIENT_LOCATION_UNAUTHENTICATED",
    "CLIENT_LOCATION_AUTHORITY_REJECTED",
    "CLIENT_LOCATION_NOT_FOUND",
    "CLIENT_LOCATION_UNAVAILABLE",
]


class ClientLocationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ClientLocationErrorCode


_BEARER = HTTPBearer(
    scheme_name="ClientSession",
    bearerFormat="opaque-client-session-v03c",
    auto_error=False,
)

_ERROR_STATUS = {
    ClientLocationInvalid: status.HTTP_400_BAD_REQUEST,
    ClientLocationStale: status.HTTP_400_BAD_REQUEST,
    ClientLocationFuture: status.HTTP_400_BAD_REQUEST,
    ClientLocationUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientLocationAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientLocationNotFound: status.HTTP_404_NOT_FOUND,
    ClientLocationDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientLocationUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS[type(error)],
        content={"code": error.code},
    )


def _response(snapshot) -> ClientLocationSnapshotResponse:
    return ClientLocationSnapshotResponse(
        contract_version="1",
        location_snapshot_id=snapshot.location_snapshot_id,
        human_identity_id=snapshot.human_identity_id,
        device_id=snapshot.device_id,
        tenant_id=snapshot.tenant_id,
        latitude=snapshot.latitude,
        longitude=snapshot.longitude,
        accuracy_m=snapshot.accuracy_m,
        precision=snapshot.precision,
        captured_at=snapshot.captured_at,
        received_at=snapshot.received_at,
    )


def build_client_location_router(
    *,
    get_session,
    service: ClientLocationService,
) -> APIRouter:
    router = APIRouter(tags=["client-location"])

    @router.put(
        "/api/v1/client/location/current",
        response_model=ClientLocationSnapshotResponse,
        responses={
            400: {"model": ClientLocationErrorResponse},
            401: {"model": ClientLocationErrorResponse},
            403: {"model": ClientLocationErrorResponse},
            503: {"model": ClientLocationErrorResponse},
        },
        operation_id="putCurrentClientLocation",
    )
    def put_current(
        payload: ClientLocationWriteRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientLocationSnapshotResponse | JSONResponse:
        token = credentials.credentials if credentials is not None else None
        try:
            with session.begin_nested():
                snapshot = service.put_current(
                    session,
                    session_token=token,
                    observation=CurrentLocationObservation(
                        latitude=payload.latitude,
                        longitude=payload.longitude,
                        accuracy_m=payload.accuracy_m,
                        captured_at=payload.captured_at,
                        precision=payload.precision,
                    ),
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _response(snapshot)

    @router.get(
        "/api/v1/client/location/current",
        response_model=ClientLocationSnapshotResponse,
        responses={
            401: {"model": ClientLocationErrorResponse},
            403: {"model": ClientLocationErrorResponse},
            404: {"model": ClientLocationErrorResponse},
            503: {"model": ClientLocationErrorResponse},
        },
        operation_id="getCurrentClientLocation",
    )
    def get_current(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientLocationSnapshotResponse | JSONResponse:
        token = credentials.credentials if credentials is not None else None
        try:
            snapshot = service.get_current(
                session,
                session_token=token,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _response(snapshot)

    return router
