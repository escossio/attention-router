"""Unregistered Client API router for synthetic D08 review contract tests.

Deliberately absent from web.app. ClientSession and unique-owner checks are
performed by the existing GovernedSemanticLab on every call, including replay.
"""
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, TypeAdapter

from attention_router.application.client_session import ClientSessionError
from attention_router.application.semantic_governance import LabOperation
from attention_router.application.semantic_interpretation import Contract, Interpretation, SourceEvidence


class ReviewRequest(Contract):
    candidate_id: str = Field(min_length=1, max_length=120)
    action: Literal['ADMIT', 'REJECT', 'CORRECT', 'CONTEST', 'SUPERSEDE', 'REVOKE_ASSOCIATION']
    decision_ref: str = Field(min_length=1, max_length=120)
    replacement_id: str | None = Field(default=None, min_length=1, max_length=120)


class ContextRequest(Contract):
    query: str = Field(min_length=1, max_length=2000, repr=False)


class ProcessMessageRequest(Contract):
    message_id: str = Field(min_length=1, max_length=120)


class CandidatePreview(Contract):
    candidate_id: str
    state: Literal['PROPOSED', 'NEEDS_REVIEW', 'ADMITTED', 'REJECTED', 'SUPERSEDED']
    decision_ref: str | None
    canonical_truth: Literal[False]
    interpretation: Interpretation
    evidence: list[SourceEvidence]


class ReviewReceipt(Contract):
    fingerprint: str
    candidate_id: str
    action: Literal['ADMIT', 'REJECT', 'CORRECT', 'CONTEST', 'SUPERSEDE', 'REVOKE_ASSOCIATION']
    replacement_id: str | None
    status: Literal['PENDING', 'COMPLETE']
    at: datetime
    owner_id: str
    source_ids: list[str]


class ContextPreview(Contract):
    graph_context: dict
    candidate_insights: list[dict]
    event_context: dict
    context_incomplete: bool
    tenant_id: str
    source_account: str
    grants_authority: Literal[False]
    policy_revision: int = Field(ge=1)
    cache_expires_at: datetime
    decisions: list[ReviewReceipt]
    context_fingerprint: str


def build_semantic_review_preview_router(*, get_session, service, clock=lambda: datetime.now(UTC),
                                        include_processing=False):
    router = APIRouter(prefix='/api/v1/client/semantic-preview', tags=['semantic-review-preview'])
    bearer = HTTPBearer(scheme_name='ClientSession', auto_error=False)

    def call(method, session, credentials, *, response_contract=None, **kwargs):
        def reply(status, content):
            return JSONResponse(status_code=status, content=content, headers={'Cache-Control': 'no-store'})
        if credentials is None:
            return reply(401, {'code': 'CLIENT_SESSION_UNAUTHENTICATED'})
        try:
            result = method(session, session_token=credentials.credentials, now=clock(), **kwargs)
            if response_contract is not None:
                result = TypeAdapter(response_contract).validate_python(result)
            from fastapi.encoders import jsonable_encoder
            return reply(200, jsonable_encoder(result))
        except ClientSessionError:
            return reply(401, {'code': 'CLIENT_SESSION_UNAUTHENTICATED'})
        except ValueError:
            return reply(409, {'code': 'SEMANTIC_REVIEW_DENIED'})
        except Exception:
            return reply(503, {'code': 'SEMANTIC_REVIEW_UNAVAILABLE'})

    @router.get('/candidates', operation_id='listSemanticPreviewCandidates', response_model=list[CandidatePreview])
    def candidates(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
        return call(service.candidates, session, credentials, response_contract=list[CandidatePreview])

    @router.get('/history', operation_id='getSemanticPreviewHistory')
    def history(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
        return call(service.history, session, credentials)

    @router.post('/review', operation_id='reviewSemanticPreviewCandidate', response_model=ReviewReceipt)
    def review(body: ReviewRequest, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
        return call(service.review, session, credentials, response_contract=ReviewReceipt, **body.model_dump())

    @router.post('/operations', operation_id='operateSemanticPreviewPolicy')
    def operation(body: LabOperation, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
        return call(service.operate, session, credentials, request=body)

    @router.post('/context', operation_id='getSemanticPreviewContext', response_model=ContextPreview)
    def context(body: ContextRequest, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
        return call(service.context, session, credentials, response_contract=ContextPreview, query=body.query)

    if include_processing:
        @router.get('/conversation', operation_id='getSemanticCanaryConversation')
        def conversation(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
            return call(service.conversation, session, credentials)

        @router.post('/process', operation_id='processSemanticCanaryMessage')
        def process(body: ProcessMessageRequest, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)], session=Depends(get_session)):
            return call(service.process_message, session, credentials, message_id=body.message_id)

    return router
