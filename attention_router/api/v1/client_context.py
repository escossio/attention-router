"""Opt-in HTTP contract for the isolated contextual query. No app registration."""

import logging
from datetime import datetime
from secrets import token_hex
from typing import Annotated, Literal

from fastapi import APIRouter, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.exc import DBAPIError

from attention_router.application.client_session import (
    ClientSessionAuthorityRejected, ClientSessionError, ClientSessionUnauthenticated,
)

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class ContextQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_ref: str = Field(pattern=r"^ctx_[A-Za-z0-9_-]{32,96}$")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class AnswerEvidence(BaseModel):
    source_ref: str
    observed_at: datetime
    excerpt: str


class ContextAnswer(BaseModel):
    kind: Literal["GOVERNED_FACT", "OBSERVATION", "HYPOTHESIS", "CONFLICT", "INSUFFICIENT_EVIDENCE"]
    conclusion: str | None = None
    date: str | None = None
    conditions: tuple[str, ...] = ()
    uncertainty: str | None = None
    evidence: tuple[AnswerEvidence, ...] = ()
    correlation_id: str


def build_client_context_router(reader) -> APIRouter:
    router = APIRouter(tags=["client-context"])

    @router.post("/api/v1/client/context/query", response_model=ContextAnswer)
    def ask(
        payload: ContextQuestion,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ):
        correlation = f"ctxq_{token_hex(16)}"

        def error(status, code):
            logger.info("context_query correlation=%s state=%s", correlation, code)
            return JSONResponse(status_code=status, content={"code": code, "correlation_id": correlation})

        try:
            answer = ContextAnswer(
                correlation_id=correlation,
                **reader.query(
                    bearer=credentials.credentials if credentials else None,
                    scope_ref=payload.scope_ref,
                    question=payload.question,
                ),
            )
        except ClientSessionUnauthenticated:
            return error(401, "CLIENT_SESSION_UNAUTHENTICATED")
        except ClientSessionAuthorityRejected:
            return error(403, "CLIENT_CONTEXT_SCOPE_DENIED")
        except (ClientSessionError, ValueError, DBAPIError, OSError):
            return error(503, "CLIENT_CONTEXT_UNAVAILABLE")
        logger.info("context_query correlation=%s state=%s", correlation, answer.kind)
        return JSONResponse(content=answer.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return router
