"""Unbound experimental ASGI composition. Importing this module opens no listener."""

import json
import uuid
from typing import TYPE_CHECKING

import anyio
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from attention_router.api.v1.client_session import build_client_session_router
from attention_router.application.client_session import (
    ClientSessionAuthorityRejected,
    ClientSessionService,
    ClientSessionUnauthenticated,
)
from .reader import AuthenticatedMemoryDelivery, MemoryAuthority, StaleEvidence
from .schema import IDENTITY_TABLES

if TYPE_CHECKING:
    from attention_router.api.v1.client_context_staged import StagedDelivery

PATH = "/api/v1/client/context/query"


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_ref: str = Field(pattern=r"^ctx_[0-9a-f]{64}$")
    question: str = Field(min_length=1, max_length=1000)


def require_experimental(factory, *, reader, onboarding=False):
    """Attest exclusive schema, role and write fences; never create/repair them."""
    with factory.begin() as session:
        database, role = session.execute(text("SELECT current_database(),current_user")).one()
        import re

        if not re.fullmatch(r"andy_memory_m1_[0-9a-f]{32}", database):
            raise ValueError("EXCLUSIVE_DATABASE_REQUIRED")
        if not re.fullmatch(r"m1_[a-z0-9_]{1,48}", role):
            raise ValueError("EXPERIMENTAL_ROLE_REQUIRED")
        if session.execute(
            text(
                "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
        ).scalar_one():
            raise ValueError("UNPRIVILEGED_ROLE_REQUIRED")
        if (
            session.execute(
                text("SELECT classification FROM memory_environment WHERE singleton")
            ).scalar_one()
            != "SYNTHETIC_M1"
        ):
            raise ValueError("SYNTHETIC_DATABASE_REQUIRED")
        protected = {t.name for t in IDENTITY_TABLES} | {
            "evidence",
            "information",
            "revision",
            "memory_scopes",
            "memory_scope_information",
        }
        if onboarding:
            protected |= {
                "external_identity_bindings",
                "human_auth_transactions",
                "human_auth_continuation_grants",
                "device_bootstrap_challenges",
            }
        tables = set(
            session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            ).scalars()
        )
        if tables != protected | {"memory_environment"}:
            raise ValueError("EXCLUSIVE_SCHEMA_REQUIRED")
        fences = set(
            session.execute(
                text("""SELECT c.relname FROM pg_trigger t
            JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND t.tgname='memory_m1_write_fence'
            AND t.tgenabled='O' AND t.tgtype=30""")
            ).scalars()
        )
        if fences != protected:
            raise ValueError("WRITE_FENCES_REQUIRED")
        if reader:
            for table in tables:
                if session.execute(
                    text(
                        "SELECT has_table_privilege(current_user,:t,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') OR has_any_column_privilege(current_user,:t,'INSERT,UPDATE')"
                    ),
                    {"t": table},
                ).scalar_one():
                    raise ValueError("READ_ONLY_ROLE_REQUIRED")
        return database, role


class ContextEndpoint:
    def __init__(self, delivery: "StagedDelivery"):
        self.delivery = delivery

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        started = False
        disconnected = anyio.Event()
        finished = anyio.Event()
        status = 503
        code = "UNAVAILABLE"

        async def error_response(status, code):
            body = {
                "contract_version": "1",
                "status": "UNAVAILABLE",
                "answer": None,
                "evidence": [],
                "correlation_id": str(uuid.uuid4()),
                "error": code,
            }
            with anyio.move_on_after(2):
                await JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})(
                    scope, receive, send
                )

        try:
            raw = bytearray()
            with anyio.fail_after(2):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 8192:
                        raise ValueError("INPUT_TOO_LARGE")
                    raw.extend(chunk)
            payload = Question.model_validate_json(raw)
        except ClientDisconnect:
            return
        except (ValueError, TimeoutError):
            await error_response(422, "INVALID_QUERY")
            return
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            await error_response(401, "UNAUTHENTICATED")
            return

        async def disclose(body):
            nonlocal started
            with anyio.fail_after(2):
                await anyio.lowlevel.checkpoint()
                if disconnected.is_set():
                    raise ClientDisconnect()
                correlation = json.loads(body)["correlation_id"]
                started = True
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"x-correlation-id", correlation.encode()),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body, "more_body": False})

        def read():
            self.delivery.query_and_deliver(
                bearer=bearer,
                scope_ref=payload.scope_ref,
                question=payload.question,
                deliver=lambda body: anyio.from_thread.run(disclose, body),
            )

        async def run():
            nonlocal status, code
            try:
                await anyio.to_thread.run_sync(read)
            except ClientSessionUnauthenticated:
                status, code = 401, "UNAUTHENTICATED"
            except ClientSessionAuthorityRejected:
                status, code = 403, "AUTHORITY_REJECTED"
            except StaleEvidence:
                status, code = 409, "EVIDENCE_SUPERSEDED"
            except ValueError:
                status, code = 422, "INVALID_QUERY"
            except Exception:
                # Never expose exception SQL, bearer, keys or evidence.
                status, code = 503, "UNAVAILABLE"
            finally:
                finished.set()

        async def watch():
            while (await receive())["type"] != "http.disconnect":
                await anyio.lowlevel.checkpoint()
            disconnected.set()

        async with anyio.create_task_group() as group:
            group.start_soon(watch)
            group.start_soon(run)
            await finished.wait()
            if not started and not disconnected.is_set():
                await error_response(status, code)
            group.cancel_scope.cancel()


def create_app(*, issuer_factory, reader_factory, settings, onboarding=False):
    def location(factory):
        url = factory.kw["bind"].url
        return url.drivername, url.host, url.port, url.database, url.query

    if location(issuer_factory) != location(reader_factory):
        raise ValueError("SAME_EXPERIMENTAL_DATABASE_REQUIRED")
    issuer_db, issuer_role = require_experimental(
        issuer_factory, reader=False, onboarding=onboarding
    )
    reader_db, reader_role = require_experimental(
        reader_factory, reader=True, onboarding=onboarding
    )
    if issuer_db != reader_db or issuer_role == reader_role:
        raise ValueError("SEPARATE_ROLES_SAME_EXPERIMENTAL_DATABASE_REQUIRED")
    service = ClientSessionService(settings=settings)

    def get_session():
        with issuer_factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout='1s'"))
            session.execute(text("SET LOCAL statement_timeout='3s'"))
            yield session

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_client_session_router(get_session=get_session, service=service))
    delivery = AuthenticatedMemoryDelivery(MemoryAuthority(reader_factory, service))
    app.router.routes.append(Route(PATH, ContextEndpoint(delivery), methods=["POST"]))

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, error):
        return JSONResponse(
            {"error": "UNAVAILABLE"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    return app
