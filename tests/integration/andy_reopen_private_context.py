"""Process boundary for a synthetic OWNER read over the private staged endpoint."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import sys

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

from attention_router.application.client_context import ClientContextReader
from attention_router.application.client_session import ClientSessionService
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_owner_scope import AuthenticatedOwnerScopeRegistry
from attention_router.application.conversation_embedding import LocalEmbeddingProvider
from attention_router.config import settings
from attention_router.infrastructure.context_epoch_durable import DurableEpochAuthority
from attention_router.web.private_context import QUERY_PATH, query_endpoint


async def reopen(fixture):
    url = make_url(fixture["database"])
    if not (url.host == "127.0.0.1" and re.fullmatch(r"attention_router_test_[0-9a-f]{10}", url.database or "")
            and re.fullmatch(r"fixture_context_[0-9a-f]{12}", url.username or "")):
        raise ValueError("disposable fixture required")
    settings.app_env = "production"
    settings.client_session_enabled = True
    settings.staged_context_private_canary_enabled = True
    settings.context_epoch_authority_canary_enabled = True
    for field in ("cognitive_runtime_canary_tenant_id", "persistent_memory_canary_binding_id",
                  "staged_context_canary_owner_id", "staged_context_canary_source_account"):
        setattr(settings, field, fixture["canary"][field])
    engine = create_engine(url, hide_parameters=True)
    try:
        def clock():
            return datetime.fromisoformat(fixture["clock"])

        def auth_clock():
            return datetime.fromisoformat(fixture["auth_clock"])

        key = bytes.fromhex(fixture["key"])
        authority_id = fixture["authority_id"]
        anchor = DurableEpochAuthority(Path(fixture["anchor"]), authority_id=authority_id, key=key)
        epoch = ContextEpoch(anchor, authority_id=authority_id, environment=fixture["environment"], key=key)
        sessions = ClientSessionService(settings=settings)
        original = ClientContextReader(sessionmaker(engine, autoflush=False), role=url.username,
                                       client_sessions=sessions, scope_registry={}, consent=None,
                                       epoch=epoch, read=None, clock=clock, auth_clock=auth_clock)
        registry = AuthenticatedOwnerScopeRegistry(client_sessions=sessions, clock=clock, auth_clock=auth_clock)
        endpoint = query_endpoint(original, LocalEmbeddingProvider(), registry)
        app = Starlette(routes=[Route(QUERY_PATH, endpoint, methods=["POST"])])
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 15432))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", trust_env=False) as client:
            response = await client.post(QUERY_PATH, json=fixture["question"],
                                         headers={"Authorization": "Bearer " + fixture["bearer"]})
            if response.status_code != 200:
                raise ValueError("private fixture did not deliver")
            return response.json()
    finally:
        engine.dispose()


if __name__ == "__main__":
    # The fixture bearer and ephemeral DB credentials are passed only through stdin.
    try:
        result = asyncio.run(reopen(json.load(sys.stdin)))
    except Exception:
        raise SystemExit("PRIVATE_REOPEN_FAILED") from None
    print(json.dumps(result))
