"""Dedicated loopback process for read-only OWNER queries. No application startup hooks."""

from collections import defaultdict, deque
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from attention_router.api.v1.client_context_staged import StagedContextEndpoint
from attention_router.application.client_context import ClientContextReader
from attention_router.application.client_session import ClientSessionService
from attention_router.application.context_canary_gate import canary_configured
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_index_fence import fence_index
from attention_router.application.context_owner_scope import AuthenticatedOwnerScopeRegistry
from attention_router.application.context_pg_consent import PgSourceConsent
from attention_router.application.context_preactivation import require_context_database
from attention_router.application.conversation_embedding import LocalEmbeddingProvider
from attention_router.application.conversation_evidence_staged import StagedConversationEvidenceReader
from attention_router.application.conversation_relevance import (
    CONTRAST_NEEDS_PAIR, contrast_topic_query, inspection_outcome,
)
from attention_router.config import settings
from attention_router.infrastructure.context_epoch_durable import DurableEpochAuthority
from attention_router.observability.context_performance import observe_database
from attention_router.observability.private_context import correlation_id, record_failure


QUERY_PATH = "/api/v1/client/context/query"
MODEL_MANIFEST = Path(__file__).resolve().parents[2] / "ops/reconciliation/model-manifest.json"
SOCKET_DIRECTORY = "/run/postgresql"


def owner_answer(packet):
    """Present supported literal excerpts, without asserting their claims as facts."""
    evidence = {}
    outcomes = defaultdict(lambda: defaultdict(list))
    topic = contrast_topic_query(getattr(packet, "query", ""))
    for item in packet.items:
        reason = item.provenance["single_excerpt_heuristic"]
        for excerpt in item.attributes["excerpts"]:
            key = (excerpt["source_ref"], excerpt["char_start"], excerpt["char_end"])
            if topic is None and reason is None and excerpt["text"].strip():
                evidence[key] = excerpt
            elif topic is not None and reason == CONTRAST_NEEDS_PAIR:
                witness = inspection_outcome(excerpt["text"], topic)
                if witness is not None:
                    subject, outcome = witness
                    outcomes[subject][outcome].append((key, excerpt))
    for group in outcomes.values():
        for approved_key, approved in group["aprovad"]:
            for refused_key, refused in group["recusad"]:
                if approved_key[0] != refused_key[0]:
                    evidence[approved_key] = approved
                    evidence[refused_key] = refused
    return {
        "kind": "OBSERVATION" if evidence else "INSUFFICIENT_EVIDENCE",
        "conclusion": None, "date": None, "conditions": [],
        "uncertainty": ("Os trechos abaixo registram o que as fontes disseram. "
                        "A conclusão permanece em aberto." if evidence
                        else "Falta evidência vigente para responder à pergunta."),
        "evidence": [
            {**{key: item[key] for key in ("source_ref", "observed_at")}, "excerpt": item["text"]}
            for item in evidence.values()
        ], "correlation_id": correlation_id(),
    }


class QueryBudget:
    """Bound encodes for the one configured OWNER of this single-worker service."""

    def __init__(self):
        self._times = deque()
        self._lock = Lock()

    def reserve(self, _scope):
        with self._lock:
            now = monotonic()
            while self._times and self._times[0] <= now - 60:
                self._times.popleft()
            if len(self._times) >= 32:
                return False
            self._times.append(now)
            return True


def _role():
    fields = (settings.cognitive_runtime_canary_tenant_id,
              settings.staged_context_canary_owner_id, settings.staged_context_canary_source_account)
    return "andy_ctx_rd_" + sha256("\0".join(fields).encode()).hexdigest()[:12]


def _secret(name):
    # Fixed paths, supplied only by the dedicated container's secret mounts.
    with (Path("/run/secrets") / name).open("rb") as stream:
        value = stream.read(4097)
    if not 0 < len(value) <= 4096:
        raise ValueError("CONTEXT_SECRET_INVALID")
    return value


def _reader_url(raw, role):
    url = make_url(raw.decode().strip())
    if (url.drivername != "postgresql+psycopg" or url.username != role or not url.database
            or url.host or url.port or url.query != {"host": SOCKET_DIRECTORY}):
        raise ValueError("CONTEXT_PRIVATE_DATABASE_INVALID")
    return url


def _prepare():
    if settings.app_env != "production" or not canary_configured():
        raise ValueError("CONTEXT_PRIVATE_GATE_INCOMPLETE")
    role = _role()
    url = _reader_url(_secret("context_reader_dsn"), role)
    authority_id = os.environ["CONTEXT_EPOCH_AUTHORITY_ID"]
    environment = os.environ["CONTEXT_EPOCH_ENVIRONMENT"]
    key = _secret("context_epoch_key")
    authority = DurableEpochAuthority(Path("/run/context-epoch/anchor.sqlite"),
                                      authority_id=authority_id, key=key)
    epoch = ContextEpoch(authority, authority_id=authority_id, environment=environment, key=key)
    model_root = Path("/opt/andy-embedding-model")
    if json.loads((model_root / "manifest.json").read_bytes()) != json.loads(MODEL_MANIFEST.read_bytes()):
        raise ValueError("CONTEXT_PRIVATE_MODEL_MANIFEST_INVALID")
    provider = LocalEmbeddingProvider(model_root)
    sessions = ClientSessionService(settings=settings)
    engine = create_engine(url, hide_parameters=True, pool_size=2, max_overflow=0, pool_timeout=2,
                           connect_args={"connect_timeout": 3, "options": "-c statement_timeout=8000 -c lock_timeout=4000"})
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        def ready():
            with factory.begin() as db:
                require_context_database(db, reader_role=role, require_pg_consent=True, require_fence=True)
                fence_index(SimpleNamespace(session=db, arguments={"tenant_id": settings.cognitive_runtime_canary_tenant_id}))
        ready()
        original = ClientContextReader(factory, role=role, client_sessions=sessions, scope_registry={},
                                       consent=None, epoch=epoch, read=None)
        endpoint = query_endpoint(original, provider, AuthenticatedOwnerScopeRegistry(client_sessions=sessions))
        return endpoint, ready, engine.dispose
    except BaseException:
        engine.dispose()
        raise


def query_endpoint(original, provider, registry):
    if settings.context_performance_enabled:
        observe_database(original._sessions.kw["bind"])
    reader = StagedConversationEvidenceReader(
        original, provider=provider, reserve_query=QueryBudget().reserve,
        owner_scope_registry=registry, pg_consent=PgSourceConsent(), render_packet=owner_answer,
    )
    return StagedContextEndpoint(reader)


def create_app():
    active = settings.staged_context_private_canary_enabled
    endpoint, check, dispose = _prepare() if active else (None, None, None)

    def live(_request):
        return JSONResponse({"status": "live"})

    def ready(_request):
        if check is None:
            return JSONResponse({"status": "disabled"}, status_code=503)
        try:
            check()
        except Exception as error:
            record_failure(error)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if dispose:
                dispose()

    routes = [Route("/health/live", live), Route("/health/ready", ready)]
    if endpoint is not None:
        routes.append(Route(QUERY_PATH, endpoint, methods=["POST"]))
    return Starlette(routes=routes, lifespan=lifespan)


def main():
    import uvicorn

    logger = logging.getLogger("attention_router.observability.private_context")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())
    logger.propagate = False
    try:
        app = create_app()
    except Exception:
        raise SystemExit("CONTEXT_PRIVATE_STARTUP_REFUSED") from None
    uvicorn.run(app, host="127.0.0.1", port=18110, workers=1, proxy_headers=False,
                access_log=False, log_level="critical", server_header=False, ws="none",
                limit_concurrency=6, backlog=8, timeout_keep_alive=2, timeout_graceful_shutdown=12)


if __name__ == "__main__":
    main()
