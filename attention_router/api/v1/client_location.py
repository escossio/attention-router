"""V0.4A authenticated current-location API."""

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

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: float = Field(gt=0, le=10_000, allow_inf_nan=False)
    captured_at: datetime
    precision: Literal["PRECISE", "APPROXIMATE"] | None = None


class ClientLocationSnapshot(BaseModel):
    contract_version: Literal["1"]
    location_snapshot_id: str
    human_identity_id: str
    device_id: str
    tenant_id: str
    latitude: float
    longitude: float
    accuracy_m: float
    precision: Literal["PRECISE", "APPROXIMATE"] | None = None
    captured_at: datetime
    received_at: datetime


class ClientLocationErrorResponse(BaseModel):
    code: Literal[
        "CLIENT_LOCATION_DISABLED",
        "CLIENT_LOCATION_INVALID",
        "CLIENT_LOCATION_STALE",
        "CLIENT_LOCATION_FUTURE",
        "CLIENT_LOCATION_UNAUTHENTICATED",
        "CLIENT_LOCATION_AUTHORITY_REJECTED",
        "CLIENT_LOCATION_NOT_FOUND",
        "CLIENT_LOCATION_UNAVAILABLE",
    ]

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
    ClientLocationDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClientLocationInvalid: status.HTTP_400_BAD_REQUEST,
    ClientLocationStale: status.HTTP_400_BAD_REQUEST,
    ClientLocationFuture: status.HTTP_400_BAD_REQUEST,
    ClientLocationUnauthenticated: status.HTTP_401_UNAUTHORIZED,
    ClientLocationAuthorityRejected: status.HTTP_403_FORBIDDEN,
    ClientLocationNotFound: status.HTTP_404_NOT_FOUND,
    ClientLocationUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS[type(error)],
        content={"code": error.code},
    )


def _snapshot(result) -> ClientLocationSnapshot:
    return ClientLocationSnapshot(
        contract_version=result.contract_version,
        location_snapshot_id=result.location_snapshot_id,
        human_identity_id=result.human_identity_id,
        device_id=result.device_id,
        tenant_id=result.tenant_id,
        latitude=result.latitude,
        longitude=result.longitude,
        accuracy_m=result.accuracy_m,
        precision=result.precision,
        captured_at=result.captured_at,
        received_at=result.received_at,
    )


def build_client_location_router(
    *,
    get_session,
    service: ClientLocationService,
) -> APIRouter:
    router = APIRouter(tags=["client-location"])

    @router.put(
        "/api/v1/client/location/current",
        response_model=ClientLocationSnapshot,
        responses={
            400: {"model": ClientLocationErrorResponse},
            401: {"model": ClientLocationErrorResponse},
            403: {"model": ClientLocationErrorResponse},
            503: {"model": ClientLocationErrorResponse},
        },
        operation_id="putCurrentClientLocation",
    )
    def put_current_location(
        payload: ClientLocationWriteRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientLocationSnapshot | JSONResponse:
        token = credentials.credentials if credentials is not None else None
        observation = CurrentLocationObservation(
            latitude=payload.latitude,
            longitude=payload.longitude,
            accuracy_m=payload.accuracy_m,
            captured_at=payload.captured_at,
            precision=(
                ClientLocationPrecision(payload.precision)
                if payload.precision is not None
                else None
            ),
        )
        try:
            with session.begin_nested():
                result = service.put_current(
                    session,
                    session_token=token,
                    observation=observation,
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _snapshot(result)

    @router.get(
        "/api/v1/client/location/current",
        response_model=ClientLocationSnapshot,
        responses={
            401: {"model": ClientLocationErrorResponse},
            403: {"model": ClientLocationErrorResponse},
            404: {"model": ClientLocationErrorResponse},
            503: {"model": ClientLocationErrorResponse},
        },
        operation_id="getCurrentClientLocation",
    )
    def get_current_location(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_BEARER),
        ],
        session: Session = Depends(get_session),
    ) -> ClientLocationSnapshot | JSONResponse:
        token = credentials.credentials if credentials is not None else None
        try:
            result = service.get_current(
                session,
                session_token=token,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return _snapshot(result)

    return router
