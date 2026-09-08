import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.application import services
from attention_router.application.voice_media import (
    MediaNotificationRetryable,
    MediaReadyNotification,
    record_media_notification,
)
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import InboundEventRow
from attention_router.infrastructure.repository import audit, mask_identifier
from attention_router.web.internal_security import verify_internal_signature


internal_adapter = InternalIngressAdapter()
internal_ingress_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="internal-ingress")
health_engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    poolclass=NullPool,
    hide_parameters=True,
)


def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_internal_event(
    body: bytes,
    x_attention_timestamp: str | None,
    x_attention_signature: str | None,
) -> dict[str, Any]:
    session = SessionLocal()
    try:
        if len(body) > settings.internal_ingress_max_body_bytes:
            audit(
                session,
                None,
                "internal_ingress_rejected",
                {"reason": "body_too_large", "bytes": len(body)},
                origin="ingress",
            )
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="request body too large")
        ok, reason = verify_internal_signature(body, x_attention_timestamp, x_attention_signature)
        if not ok:
            audit(session, None, "internal_ingress_auth_failed", {"reason": reason}, origin="ingress")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            audit(session, None, "internal_ingress_rejected", {"reason": "invalid_json"}, origin="ingress")
            raise HTTPException(status_code=400, detail="invalid json") from exc
        try:
            event = internal_adapter.normalize(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            audit(session, None, "internal_ingress_rejected", {"reason": "invalid_payload"}, origin="ingress")
            raise HTTPException(status_code=422, detail="invalid internal event") from exc
        audit(
            session,
            None,
            "internal_ingress_received",
            {
                "source": event.source,
                "external_event_id": event.external_event_id,
                "external_actor_id": mask_identifier(event.actor_id),
                "message_type": event.metadata.get("message_type"),
                "has_media": event.metadata.get("has_media"),
            },
            event.correlation_id,
            None,
            origin="ingress",
        )
        existing = session.query(InboundEventRow).filter_by(
            source=event.source, external_event_id=event.external_event_id
        ).first()
        try:
            result = services.receive_normalized_inbound_event(session, event)
        except services.DuplicatePayloadConflictError as exc:
            audit(
                session,
                existing.interaction_id if existing else None,
                "internal_event_conflict",
                {"source": event.source, "external_event_id": event.external_event_id},
                event.correlation_id,
                None,
                origin="ingress",
            )
            raise HTTPException(status_code=409, detail="event id conflict") from exc
        status_value = "duplicate" if existing else "accepted"
        audit_type = "internal_event_replay" if existing else "internal_event_accepted"
        audit(
            session,
            result.get("id"),
            audit_type,
            {"source": event.source, "external_event_id": event.external_event_id},
            result.get("correlation_id", event.correlation_id),
            None,
            origin="ingress",
        )
        session.commit()
        return {
            "status": status_value,
            "interaction_id": result.get("id"),
            "correlation_id": result.get("correlation_id", event.correlation_id),
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def process_media_notification(
    body: bytes,
    x_attention_timestamp: str | None,
    x_attention_signature: str | None,
) -> dict[str, Any]:
    if len(body) > settings.internal_ingress_max_body_bytes:
        raise HTTPException(status_code=413, detail="request body too large")
    ok, _reason = verify_internal_signature(body, x_attention_timestamp, x_attention_signature)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid internal signature")
    try:
        payload = MediaReadyNotification.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid media notification") from exc
    session = SessionLocal()
    try:
        artifact = record_media_notification(session, payload)
        session.commit()
        return {"status": "accepted", "artifact_id": artifact.id if artifact else None}
    except MediaNotificationRetryable as exc:
        session.rollback()
        raise HTTPException(status_code=425, detail="inbound event not ready") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="media notification conflict") from exc
    finally:
        session.close()


def create_internal_ingress_app() -> FastAPI:
    app = FastAPI(
        title="Attention Router Internal Ingress",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "live", "listener": "internal-ingress"}

    @app.get("/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        with health_engine.connect() as connection:
            connection.execute(text("select 1"))
        return {"status": "ready", "listener": "internal-ingress"}

    @app.post("/api/v1/ingress/internal/events", tags=["internal-ingress"])
    async def internal_events(
        request: Request,
        x_attention_timestamp: Annotated[str | None, Header(alias="X-Attention-Timestamp")] = None,
        x_attention_signature: Annotated[str | None, Header(alias="X-Attention-Signature")] = None,
    ) -> dict[str, Any]:
        body = await request.body()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            internal_ingress_executor,
            process_internal_event,
            body,
            x_attention_timestamp,
            x_attention_signature,
        )

    @app.post("/internal/whatsapp/media", tags=["internal-ingress"])
    async def whatsapp_media(
        request: Request,
        x_attention_timestamp: Annotated[str | None, Header(alias="X-Attention-Timestamp")] = None,
        x_attention_signature: Annotated[str | None, Header(alias="X-Attention-Signature")] = None,
    ) -> dict[str, Any]:
        body = await request.body()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            internal_ingress_executor,
            process_media_notification,
            body,
            x_attention_timestamp,
            x_attention_signature,
        )

    return app


app = create_internal_ingress_app()
