"""Separately gated, passive local vector runtime; never starts cognitive workers."""

from datetime import datetime
import logging
import os
import threading
from typing import Annotated, Literal
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from attention_router.application.conversation_vectors import cycle, sources, sql, scope
from attention_router.application.conversation_hybrid import search
from attention_router.infrastructure.maintenance_gate import runtime_admission, observe_runtime

LOG = logging.getLogger(__name__)


class VectorSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)
    source_account: str = Field(min_length=1, max_length=180)
    query: str = Field(min_length=1, max_length=4000)
    search_mode: Literal["hybrid", "vector", "lexical"] = "hybrid"
    conversation_id: str | None = None
    participant: str | None = None
    direction: str | None = None
    message_type: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    limit: int = Field(default=10, ge=1, le=50)


def metrics(session, tenant, account):
    scope(session, tenant, account)
    conversations = (
        sql(
            session,
            "SELECT id FROM conversation_threads WHERE tenant_id=:t AND source_account=:a AND source='wwebjs'",
            t=tenant,
            a=account,
        )
        .scalars()
        .all()
    )
    coverage = []
    for cid in conversations:
        eligible = len(sources(session, tenant, account, cid))
        indexed = sql(
            session,
            "SELECT count(DISTINCT m.message_id) FROM conversation_vector_memberships m JOIN conversation_vector_segments s ON s.id=m.segment_id WHERE s.tenant_id=:t AND s.source_account=:a AND s.conversation_id=:c AND s.state='READY'",
            t=tenant,
            a=account,
            c=cid,
        ).scalar()
        coverage.append(
            {"conversation_id": cid, "eligible_messages": eligible, "indexed_messages": indexed}
        )
    counts = (
        sql(
            session,
            "SELECT state,count(*) AS count FROM conversation_vector_segments WHERE tenant_id=:t AND source_account=:a GROUP BY state",
            t=tenant,
            a=account,
        )
        .mappings()
        .all()
    )
    embeddings = sql(
        session,
        "SELECT count(*) FROM conversation_embeddings WHERE tenant_id=:t AND source_account=:a",
        t=tenant,
        a=account,
    ).scalar()
    jobs = (
        sql(
            session,
            "SELECT status,count(*) AS count FROM queue WHERE kind='conversation_vector_v1' AND payload->>'tenant_id'=:t AND payload->>'source_account'=:a GROUP BY status",
            t=tenant,
            a=account,
        )
        .mappings()
        .all()
    )
    return {
        "scope": {"tenant_id": tenant, "source_account": account},
        "coverage": coverage,
        "segments": [dict(c) for c in counts],
        "embeddings": embeddings,
        "jobs": [dict(j) for j in jobs],
        "eligible_messages": sum(c["eligible_messages"] for c in coverage),
        "indexed_messages": sum(c["indexed_messages"] for c in coverage),
        "external_provider_cost": 0,
        "provider": "LOCAL_OFFLINE",
        "cognitive_integration": False,
        "global_index_bytes": sql(
            session,
            "SELECT sum(pg_total_relation_size(x::regclass)) FROM unnest(ARRAY['conversation_vector_segments','conversation_vector_memberships','conversation_embeddings']) x",
        ).scalar(),
        "global_processing_counters": dict(
            sql(session, "SELECT name,value FROM conversation_vector_stats").all()
        ),
    }


def install(app, settings, get_session, require_admin):
    def strict_admin(authorization: Annotated[str | None, Header()] = None):
        if not settings.admin_auth_enabled or not settings.admin_token:
            raise HTTPException(503, "VECTOR_ADMIN_AUTH_REQUIRED")
        require_admin(authorization)

    @app.post("/api/v1/private/conversation-search", dependencies=[Depends(strict_admin)])
    def vector_search(payload: VectorSearchRequest, session=Depends(get_session)):
        if os.getenv("CONVERSATION_VECTOR_ENABLED") != "true":
            raise HTTPException(503, "VECTOR_DISABLED")
        try:
            return search(session, **payload.model_dump())
        except PermissionError:
            raise HTTPException(403, "VECTOR_SCOPE_REVOKED") from None
        except ValueError:
            raise HTTPException(422, "VECTOR_QUERY_INVALID") from None

    @app.get("/api/v1/private/conversation-index/metrics", dependencies=[Depends(strict_admin)])
    def vector_metrics(tenant_id: str, source_account: str, session=Depends(get_session)):
        return metrics(session, tenant_id, source_account)

    stop = threading.Event()

    @app.on_event("startup")
    def start():
        if os.getenv("CONVERSATION_VECTOR_ENABLED") != "true":
            return
        from attention_router.infrastructure.db import SessionLocal

        observe_runtime("vector-cycle")

        def run():
            while not stop.is_set():
                try:
                    with runtime_admission("vector-cycle") as admitted:
                        if admitted:
                            cycle(SessionLocal, batch_size=4)
                except Exception:
                    LOG.warning("passive_vector_cycle_failed")
                stop.wait(max(10, int(os.getenv("CONVERSATION_VECTOR_INTERVAL_SECONDS", "10"))))

        threading.Thread(target=run, name="passive-vector-index", daemon=True).start()

    @app.on_event("shutdown")
    def shutdown():
        stop.set()
