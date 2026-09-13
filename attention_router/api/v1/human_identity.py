"""Public Human Identity boundary with caller-owned transaction lifecycle."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from attention_router.application.human_identity import HumanIdentityService
from attention_router.core.human_identity import (
    HumanAuthChallengeConsumed,
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
    challenge_id: str = Field(pattern="^hac_[A-Za-z0-9_-]{20,}$")
    nonce: str = Field(min_length=32, max_length=256)
    expires_at: datetime


class HumanIdentityValidatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["HUMAN_IDENTITY_VALIDATED"]
    human_identity_id: str = Field(pattern="^hid_[A-Za-z0-9_-]{20,}$")


class HumanAuthErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal[
        "HUMAN_AUTH_DISABLED",
        "HUMAN_AUTH_CHALLENGE_NOT_FOUND",
        "HUMAN_AUTH_CHALLENGE_EXPIRED",
        "HUMAN_AUTH_CHALLENGE_CONSUMED",
        "HUMAN_AUTH_CREDENTIAL_REJECTED",
        "HUMAN_AUTH_NONCE_MISMATCH",
        "HUMAN_AUTH_PROVIDER_UNAVAILABLE",
    ]


_ERROR_STATUS = {
    HumanAuthCredentialRejected: 401,
    HumanAuthNonceMismatch: 401,
    HumanAuthChallengeNotFound: 404,
    HumanAuthChallengeConsumed: 409,
    HumanAuthChallengeExpired: 410,
    HumanAuthProviderUnavailable: 503,
    HumanAuthDisabled: 503,
}
_HUMAN_AUTH_ERRORS = tuple(_ERROR_STATUS)


def _error_response(error) -> JSONResponse:
    return JSONResponse(status_code=_ERROR_STATUS[type(error)], content={"code": error.code})


def build_human_identity_router(*, get_session, service: HumanIdentityService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth/google/challenges")

    @router.post(
        "",
        status_code=201,
        response_model=HumanAuthChallengeResponse,
        responses={503: {"model": HumanAuthErrorResponse}},
        operation_id="createGoogleHumanAuthChallenge",
        openapi_extra={"security": []},
    )
    def issue_google_challenge(session: Session = Depends(get_session)):
        try:
            return service.issue_google_challenge(session)
        except _HUMAN_AUTH_ERRORS as error:
            return _error_response(error)

    @router.post(
        "/{challenge_id}/verify",
        response_model=HumanIdentityValidatedResponse,
        responses={code: {"model": HumanAuthErrorResponse} for code in (401, 404, 409, 410, 503)},
        operation_id="verifyGoogleHumanIdentity",
        openapi_extra={"security": []},
    )
    def verify_google_challenge(
        challenge_id: Annotated[str, Path(pattern="^hac_[A-Za-z0-9_-]{20,}$")],
        payload: GoogleHumanIdentityVerifyRequest,
        session: Session = Depends(get_session),
    ):
        try:
            return service.verify_google_challenge(session, challenge_id, payload.id_token)
        except _HUMAN_AUTH_ERRORS as error:
            # Return normally so the yield dependency commits deterministic rejections.
            return _error_response(error)

    return router
