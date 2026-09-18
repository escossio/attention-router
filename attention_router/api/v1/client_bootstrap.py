"""HTTP boundary for V0.3B pre-session device bootstrap."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.client_bootstrap import (
    DeviceBootstrapChallengeConsumed,
    DeviceBootstrapChallengeExpired,
    DeviceBootstrapChallengeNotFound,
    DeviceBootstrapDeviceConflict,
    DeviceBootstrapDeviceKeyInvalid,
    DeviceBootstrapDisabled,
    DeviceBootstrapGrantRejected,
    DeviceBootstrapMembershipConflict,
    DeviceBootstrapService,
    DeviceBootstrapSignatureInvalid,
    DeviceBootstrapUnavailable,
)


class DeviceBootstrapChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    continuation_token: str = Field(
        min_length=47,
        max_length=47,
        pattern=r"^hcg_[A-Za-z0-9_-]{43}$",
        repr=False,
    )
    public_key_spki_b64url: str = Field(
        min_length=80,
        max_length=2048,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    canonical_device_name: str = Field(min_length=1, max_length=160)
    platform: Literal["ANDROID"]
    roles: list[Literal["CLIENT", "CAPABILITY_NODE"]] = Field(
        min_length=1,
        max_length=2,
    )


class DeviceBootstrapChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_challenge_id: str = Field(pattern=r"^dbc_[A-Za-z0-9_-]{20,}$")
    challenge_b64url: str = Field(
        min_length=32,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    expires_at: datetime


class DeviceBootstrapCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_signature_b64url: str = Field(
        min_length=64,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
        repr=False,
    )


class ClientTenantMembershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    membership_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    role: Literal["OWNER", "ADMIN", "MEMBER"]
    status: Literal["ACTIVE"]


class ClientDeviceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(pattern=r"^cdev_[A-Za-z0-9_-]{20,}$")
    public_key_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    canonical_name: str = Field(min_length=1, max_length=160)
    platform: Literal["ANDROID"]
    roles: list[Literal["CLIENT", "CAPABILITY_NODE"]] = Field(
        min_length=1,
        max_length=2,
    )
    status: Literal["ACTIVE"]


class DeviceBootstrapEstablishedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["DEVICE_BOOTSTRAP_ESTABLISHED"]
    human_identity_id: str = Field(pattern=r"^hid_[A-Za-z0-9_-]{20,}$")
    memberships: list[ClientTenantMembershipResponse] = Field(min_length=1, max_length=100)
    initial_tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    device: ClientDeviceResponse


DeviceBootstrapErrorCode = Literal[
    "DEVICE_BOOTSTRAP_GRANT_REJECTED",
    "DEVICE_BOOTSTRAP_CHALLENGE_NOT_FOUND",
    "DEVICE_BOOTSTRAP_CHALLENGE_EXPIRED",
    "DEVICE_BOOTSTRAP_CHALLENGE_CONSUMED",
    "DEVICE_BOOTSTRAP_DEVICE_KEY_INVALID",
    "DEVICE_BOOTSTRAP_SIGNATURE_INVALID",
    "DEVICE_BOOTSTRAP_MEMBERSHIP_CONFLICT",
    "DEVICE_BOOTSTRAP_DEVICE_CONFLICT",
    "DEVICE_BOOTSTRAP_UNAVAILABLE",
]


class DeviceBootstrapErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: DeviceBootstrapErrorCode


_ERROR_STATUS = {
    DeviceBootstrapGrantRejected: status.HTTP_401_UNAUTHORIZED,
    DeviceBootstrapChallengeNotFound: status.HTTP_404_NOT_FOUND,
    DeviceBootstrapChallengeExpired: status.HTTP_410_GONE,
    DeviceBootstrapChallengeConsumed: status.HTTP_409_CONFLICT,
    DeviceBootstrapDeviceKeyInvalid: status.HTTP_400_BAD_REQUEST,
    DeviceBootstrapSignatureInvalid: status.HTTP_401_UNAUTHORIZED,
    DeviceBootstrapMembershipConflict: status.HTTP_409_CONFLICT,
    DeviceBootstrapDeviceConflict: status.HTTP_409_CONFLICT,
    DeviceBootstrapDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    DeviceBootstrapUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS[type(error)],
        content={"code": error.code},
    )


def build_client_bootstrap_router(
    *,
    get_session,
    service: DeviceBootstrapService,
) -> APIRouter:
    router = APIRouter(tags=["client-bootstrap"])

    @router.post(
        "/api/v1/bootstrap/device/challenges",
        response_model=DeviceBootstrapChallengeResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": DeviceBootstrapErrorResponse},
            401: {"model": DeviceBootstrapErrorResponse},
            409: {"model": DeviceBootstrapErrorResponse},
            503: {"model": DeviceBootstrapErrorResponse},
        },
        operation_id="createDeviceBootstrapChallenge",
        openapi_extra={"security": []},
    )
    def start_device_bootstrap(
        payload: DeviceBootstrapChallengeRequest,
        session: Session = Depends(get_session),
    ) -> DeviceBootstrapChallengeResponse | JSONResponse:
        try:
            result = service.start_device_bootstrap(
                session,
                continuation_token=payload.continuation_token,
                public_key_spki_b64url=payload.public_key_spki_b64url,
                canonical_device_name=payload.canonical_device_name,
                platform=payload.platform,
                roles=list(payload.roles),
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return DeviceBootstrapChallengeResponse(
            bootstrap_challenge_id=result.bootstrap_challenge_id,
            challenge_b64url=result.challenge_b64url,
            expires_at=result.expires_at,
        )

    @router.post(
        "/api/v1/bootstrap/device/challenges/{bootstrap_challenge_id}/complete",
        response_model=DeviceBootstrapEstablishedResponse,
        responses={
            400: {"model": DeviceBootstrapErrorResponse},
            401: {"model": DeviceBootstrapErrorResponse},
            404: {"model": DeviceBootstrapErrorResponse},
            409: {"model": DeviceBootstrapErrorResponse},
            410: {"model": DeviceBootstrapErrorResponse},
            503: {"model": DeviceBootstrapErrorResponse},
        },
        operation_id="completeDeviceBootstrap",
        openapi_extra={"security": []},
    )
    def complete_device_bootstrap(
        bootstrap_challenge_id: Annotated[
            str,
            Path(pattern=r"^dbc_[A-Za-z0-9_-]{20,}$"),
        ],
        payload: DeviceBootstrapCompleteRequest,
        session: Session = Depends(get_session),
    ) -> DeviceBootstrapEstablishedResponse | JSONResponse:
        try:
            result = service.complete_device_bootstrap(
                session,
                bootstrap_challenge_id=bootstrap_challenge_id,
                device_signature_b64url=payload.device_signature_b64url,
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return DeviceBootstrapEstablishedResponse(
            status=result.status,
            human_identity_id=result.human_identity_id,
            memberships=[
                ClientTenantMembershipResponse(
                    membership_id=item.membership_id,
                    tenant_id=item.tenant_id,
                    role=item.role.value,
                    status=item.status.value,
                )
                for item in result.memberships
            ],
            initial_tenant_id=result.initial_tenant_id,
            device=ClientDeviceResponse(
                device_id=result.device.device_id,
                public_key_fingerprint=result.device.public_key_fingerprint,
                canonical_name=result.device.canonical_name,
                platform=result.device.platform.value,
                roles=[role.value for role in result.device.roles],
                status=result.device.status.value,
            ),
        )

    return router
