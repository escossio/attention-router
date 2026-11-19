"""Owner-authenticated Contacts endpoints; queries and codes stay out of URLs."""

from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Security
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from attention_router.application.client_session import ClientSessionError
from attention_router.api.v1.gmail_connection import _session_status


class PrivateContactsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"code": "CONTACTS_INVALID_REQUEST"})

        return private_handler


class ConnectContacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: bool
    authorization_code: str = Field(min_length=8, max_length=8192, repr=False)


class SearchContacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=240, repr=False)
    kind: Literal["name", "phone", "email"] = "name"
    limit: int = Field(default=30, ge=1, le=100)


class LookupContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=1, max_length=80, repr=False)


def build_google_contacts_router(*, get_session, service):
    router = APIRouter(
        prefix="/api/v1/integrations/contacts",
        tags=["google-contacts"],
        route_class=PrivateContactsRoute,
    )
    bearer = HTTPBearer(scheme_name="ClientSession", auto_error=False)

    def call(method, session, credentials, **kwargs):
        if credentials is None:
            return JSONResponse(status_code=401, content={"code": "CLIENT_SESSION_UNAUTHENTICATED"})
        try:
            return method(session, session_token=credentials.credentials, **kwargs)
        except ClientSessionError as exc:
            return JSONResponse(status_code=_session_status(exc), content={"code": exc.code})
        except ValueError:
            return JSONResponse(status_code=409, content={"code": "CONTACTS_REQUEST_REJECTED"})
        except Exception:
            return JSONResponse(status_code=503, content={"code": "CONTACTS_UNAVAILABLE"})

    @router.post("")
    def connect(
        body: ConnectContacts,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.connect, session, credentials, **body.model_dump())

    @router.get("")
    def status(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.status, session, credentials)

    @router.post("/search")
    def search(
        body: SearchContacts,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.search, session, credentials, **body.model_dump())

    @router.post("/lookup")
    def lookup(
        body: LookupContact,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.lookup, session, credentials, **body.model_dump())

    @router.post("/match-whatsapp")
    def match_whatsapp(
        body: LookupContact,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.match_whatsapp, session, credentials, **body.model_dump())

    @router.post("/sync")
    def sync(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.request_sync, session, credentials)

    @router.delete("")
    def disconnect(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
        session=Depends(get_session),
    ):
        return call(service.revoke, session, credentials)

    return router
