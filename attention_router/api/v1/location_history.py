"""Bounded idempotent history ingestion; coordinates never appear in error responses."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Security
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from attention_router.api.v1.client_location import _BEARER, _ERROR_STATUS, _error_response
from attention_router.application.location_history import (
    HistoryInvalid,
    HistoryConflict,
    LocationHistoryService,
)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_id: UUID
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False, repr=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False, repr=False)
    accuracy_m: float = Field(gt=0, le=10000, allow_inf_nan=False)
    captured_at: datetime
    precision: Literal["PRECISE", "APPROXIMATE"]
    provider: Literal["gps", "network", "passive", "fused"]


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    human_identity_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=64)
    observations: list[Observation] = Field(min_length=1, max_length=25, repr=False)


class PrivateHistoryRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"code": "LOCATION_HISTORY_INVALID"})

        return handler


def build_location_history_router(*, get_session, service):
    router = APIRouter(tags=["client-location-history"], route_class=PrivateHistoryRoute)

    @router.post("/api/v1/client/location/observations")
    def ingest(
        payload: ObservationBatch,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
        session=Depends(get_session),
    ):
        try:
            with session.begin_nested():
                result = service.ingest(
                    session,
                    session_token=credentials.credentials if credentials else None,
                    batch=payload,
                )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        except HistoryInvalid:
            return JSONResponse(status_code=400, content={"code": "LOCATION_HISTORY_TIME_INVALID"})
        except HistoryConflict:
            return JSONResponse(status_code=409, content={"code": "LOCATION_HISTORY_ID_CONFLICT"})
        # Confirm durable persistence before acknowledging any observation.
        session.commit()
        return result

    return router


def install_location_history(app, settings, client_sessions, get_session):
    app.include_router(
        build_location_history_router(
            get_session=get_session,
            service=LocationHistoryService(settings=settings, session_service=client_sessions),
        )
    )
