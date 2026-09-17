"""HTTP boundary for the pre-session Human Identity V1 flow."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.human_identity import HumanIdentityService
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
    HumanIdentityContinued,
    HumanAuthChallengeExpired,
    HumanAuthChallengeNotFound,
    HumanAuthCredentialRejected,
    HumanAuthDisabled,
    HumanAuthNonceMismatch,
    HumanAuthProviderUnavailable,
)


class GoogleHumanIdentityVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=32, max_length=8192)


class HumanAuthChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(pattern=r"^hac_[A-Za-z0-9_-]{20,}$")
    nonce: str = Field(min_length=32, max_length=256)
    expires_at: datetime


class HumanIdentityValidatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["HUMAN_IDENTITY_VALIDATED"]
    human_identity_id: str = Field(
        pattern=r"^hid_[A-Za-z0-9_-]{20,}$",
        description="Opaque Human Identity reference only; not a credential or authority.",
    )


class HumanAuthContinuationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(
        min_length=47, max_length=47, pattern=r"^hcg_[A-Za-z0-9_-]{43}$", repr=False,
        description="Sensitive opaque continuation credential; do not log or persist on clients.",
    )
    purpose: Literal["DEVICE_BOOTSTRAP"]
    expires_at: datetime


class HumanIdentityContinuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["HUMAN_IDENTITY_VALIDATED"]
    # This is an opaque reference, not an authentication credential.
    human_identity_id: str = Field(
        pattern=r"^hid_[A-Za-z0-9_-]{20,}$",
        description="Opaque Human Identity reference only; not a credential or authority.",
    )
    continuation_grant: HumanAuthContinuationGrant


HumanAuthErrorCode = Literal[
    "HUMAN_AUTH_DISABLED",
    "HUMAN_AUTH_CHALLENGE_NOT_FOUND",
    "HUMAN_AUTH_CHALLENGE_EXPIRED",
    "HUMAN_AUTH_CHALLENGE_CONSUMED",
    "HUMAN_AUTH_CREDENTIAL_REJECTED",
    "HUMAN_AUTH_NONCE_MISMATCH",
    "HUMAN_AUTH_PROVIDER_UNAVAILABLE",
]


class HumanAuthErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: HumanAuthErrorCode


_ERROR_STATUS = {
    HumanAuthChallengeNotFound: status.HTTP_404_NOT_FOUND,
    HumanAuthChallengeExpired: status.HTTP_410_GONE,
    HumanAuthChallengeConsumed: status.HTTP_409_CONFLICT,
    HumanAuthCredentialRejected: status.HTTP_401_UNAUTHORIZED,
    HumanAuthNonceMismatch: status.HTTP_401_UNAUTHORIZED,
    HumanAuthProviderUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    HumanAuthDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS[type(error)],
        content={"code": error.code},
    )


def build_human_identity_router(*, get_session, service: HumanIdentityService) -> APIRouter:
    router = APIRouter(tags=["human-identity"])

    @router.post(
        "/api/v1/auth/google/challenges",
        response_model=HumanAuthChallengeResponse,
        status_code=status.HTTP_201_CREATED,
        responses={503: {"model": HumanAuthErrorResponse}},
        operation_id="createGoogleHumanAuthChallenge",
        openapi_extra={"security": []},
    )
    def issue_google_challenge(
        session: Session = Depends(get_session),
    ) -> HumanAuthChallengeResponse | JSONResponse:
        try:
            issued = service.issue_google_challenge(session)
        except HumanAuthDisabled as error:
            return _error_response(error)
        return HumanAuthChallengeResponse(
            challenge_id=issued.challenge_id,
            nonce=issued.nonce,
            expires_at=issued.expires_at,
        )

    @router.post(
        "/api/v1/auth/google/challenges/{challenge_id}/verify",
        response_model=HumanIdentityValidatedResponse,
        responses={
            401: {"model": HumanAuthErrorResponse},
            404: {"model": HumanAuthErrorResponse},
            409: {"model": HumanAuthErrorResponse},
            410: {"model": HumanAuthErrorResponse},
            503: {"model": HumanAuthErrorResponse},
        },
        operation_id="verifyGoogleHumanIdentity",
        openapi_extra={"security": []},
    )
    def verify_google_challenge(
        challenge_id: Annotated[str, Path(pattern=r"^hac_[A-Za-z0-9_-]{20,}$")],
        payload: GoogleHumanIdentityVerifyRequest,
        session: Session = Depends(get_session),
    ) -> HumanIdentityValidatedResponse | JSONResponse:
        try:
            validated = service.verify_google_challenge(
                session, challenge_id, payload.id_token
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return HumanIdentityValidatedResponse(
            status=validated.status,
            human_identity_id=validated.human_identity_id,
        )

    @router.post(
        "/api/v1/auth/google/challenges/{challenge_id}/verify-and-continue",
        response_model=HumanIdentityContinuedResponse,
        responses={
            401: {"model": HumanAuthErrorResponse},
            404: {"model": HumanAuthErrorResponse},
            409: {"model": HumanAuthErrorResponse},
            410: {"model": HumanAuthErrorResponse},
            503: {"model": HumanAuthErrorResponse},
        },
        operation_id="verifyGoogleHumanIdentityAndIssueContinuationGrant",
        openapi_extra={"security": []},
    )
    def verify_google_challenge_and_continue(
        challenge_id: Annotated[str, Path(pattern=r"^hac_[A-Za-z0-9_-]{20,}$")],
        payload: GoogleHumanIdentityVerifyRequest,
        session: Session = Depends(get_session),
    ) -> HumanIdentityContinuedResponse | JSONResponse:
        try:
            continued: HumanIdentityContinued = service.verify_google_challenge_and_issue_continuation_grant(
                session, challenge_id, payload.id_token
            )
        except tuple(_ERROR_STATUS) as error:
            return _error_response(error)
        return HumanIdentityContinuedResponse(
            status=continued.status,
            human_identity_id=continued.human_identity_id,
            continuation_grant=HumanAuthContinuationGrant(
                token=continued.continuation_grant.token,
                purpose=continued.continuation_grant.purpose,
                expires_at=continued.continuation_grant.expires_at,
            ),
        )

    return router
