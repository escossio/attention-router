"""Loopback-only passive search/index process; no operational/cognitive workers."""

import hmac
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.conversation_vector_runtime import install
from attention_router.application.conversation_embedding import get_provider

app = FastAPI(
    title="Andy Passive Conversation Index", docs_url=None, redoc_url=None, openapi_url=None
)


def get_session():
    with SessionLocal() as session:
        yield session


def require_admin(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "admin credential required")
    if not settings.admin_token or not hmac.compare_digest(
        authorization[7:].strip(), settings.admin_token
    ):
        raise HTTPException(403, "admin credential invalid")


@app.on_event("startup")
def warm_model():
    get_provider()


@app.get("/health/ready")
def ready():
    with SessionLocal() as s:
        s.execute(text("SELECT 1 FROM conversation_embeddings LIMIT 0"))
    return {"status": "ready", "mode": "PASSIVE_LOCAL_VECTOR", "cognitive_execution": False}


install(app, settings, get_session, require_admin)
