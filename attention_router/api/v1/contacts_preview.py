"""Owner-authenticated, opt-in Contacts preview; no personal data in URL queries."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from attention_router.application.client_session import ClientSessionError
from attention_router.api.v1.gmail_connection import _session_status


class ContactsConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["CONNECTED_EPHEMERAL", "DISCONNECTED"]
    scope: str | None = None


class ContactsLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["MATCH", "NOT_FOUND", "AMBIGUOUS", "INCOMPLETE", "INVALID_PHONE"]
    resource: str | None = None
    saved_as: str | None = None
    source: Literal["GOOGLE_CONTACTS"] | None = None
    identity_verified: bool = False
    etag: str | None = None
    phone: str | None = None


class BilateralPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PREVIEW_ONLY", "NOT_FOUND", "AMBIGUOUS", "LOOKUP_LIMIT"]
    conversation_id: str | None = None
    conversation_key: str | None = None
    messages: list[dict] = Field(default_factory=list)
    participants: list[dict] = Field(default_factory=list)
    associations: list[dict] = Field(default_factory=list)
    episodes: list[dict] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)
    context_incomplete: bool = True
    truncated: bool = False
    durable_claims: list = Field(default_factory=list, max_length=0)
    inferences: list = Field(default_factory=list, max_length=0)


class ConnectContacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_code: str = Field(min_length=8, max_length=8192, repr=False)
    consent: bool


class LookupContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=8, max_length=32, repr=False)


class ConversationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account: str = Field(min_length=1, max_length=180)
    identifier: str = Field(min_length=1, max_length=240, repr=False)


class AliasReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account: str = Field(min_length=1, max_length=180)
    message_id: str = Field(min_length=1, max_length=64)
    active: bool
    decision_ref: str = Field(min_length=1, max_length=120)


class RetryTranscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transcription_id: str = Field(min_length=1, max_length=64)


def build_contacts_preview_router(*, get_session, service):
    router = APIRouter(prefix="/api/v1/integrations/contacts", tags=["contacts-preview"])
    bearer = HTTPBearer(scheme_name="ClientSession", auto_error=False)

    def call(operation, session, credentials, **kwargs):
        if credentials is None:
            return JSONResponse(status_code=401, content={"code": "CLIENT_SESSION_UNAUTHENTICATED"})
        try:
            return operation(session, session_token=credentials.credentials, **kwargs)
        except ClientSessionError as error:
            return JSONResponse(status_code=_session_status(error), content={"code": error.code})
        except ValueError:
            return JSONResponse(status_code=409, content={"code": "CONTACTS_REQUEST_REJECTED"})
        except Exception:
            return JSONResponse(status_code=503, content={"code": "CONTACTS_UNAVAILABLE"})

    @router.post("", operation_id="connectContactsPreview", response_model=ContactsConnectionResponse, response_model_exclude_none=True)
    def connect(body: ConnectContacts,
                credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
                session=Depends(get_session)):
        return call(service.connect, session, credentials, consent=body.consent,
                    authorization_code=body.authorization_code)

    @router.post("/lookup", operation_id="lookupContactsPreview", response_model=ContactsLookupResponse, response_model_exclude_none=True)
    def lookup(body: LookupContact,
               credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
               session=Depends(get_session)):
        return call(service.lookup, session, credentials, phone=body.phone)

    @router.delete("", operation_id="revokeContactsPreview", response_model=ContactsConnectionResponse, response_model_exclude_none=True)
    def revoke(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
               session=Depends(get_session)):
        return call(service.revoke, session, credentials)

    @router.post("/conversation", operation_id="getBilateralConversationPreview", response_model=BilateralPreviewResponse, response_model_exclude_none=True)
    def conversation(body: ConversationPreviewRequest,
                     credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
                     session=Depends(get_session)):
        return call(service.conversation, session, credentials, **body.model_dump())

    @router.post("/aliases/review", operation_id="reviewBilateralAlias")
    def review_alias(body: AliasReviewRequest,
                     credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
                     session=Depends(get_session)):
        return call(service.review_alias, session, credentials, **body.model_dump())

    @router.post("/conversation/retry-transcription", operation_id="retryBilateralTranscription")
    def retry_transcription(body: RetryTranscriptionRequest,
                            credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
                            session=Depends(get_session)):
        return call(service.retry_transcription, session, credentials, **body.model_dump())

    return router
