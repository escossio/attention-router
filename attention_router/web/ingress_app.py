import hashlib
import hmac
import json
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.adapters.meta_whatsapp import MetaWhatsAppInboundAdapter
from attention_router.application import services
from attention_router.config import settings
from attention_router.domain.models import now_utc
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import AuditEventRow, InboundEventRow
from attention_router.infrastructure.repository import audit
from attention_router.platform.human_authorization_control_gate import handle_meta_control_event
from attention_router.platform.meta_callback_reconciliation import (
    MetaAdmissionUnavailable,
    admit_meta_callback_evidence,
    reconcile_meta_callback_outcome,
)
from attention_router.platform.meta_observability import (
    provider_message_id_fingerprint,
)
from attention_router.web.meta_security import verify_meta_signature


meta_adapter = MetaWhatsAppInboundAdapter()


def _shadow_event_payload(event: Any) -> dict[str, Any]:
    metadata = event.metadata
    return {
        "sender": metadata.get("sender"),
        "message_type": metadata.get("message_type"),
        "interactive_type": metadata.get("interactive_type"),
        "button_reply_id": metadata.get("button_reply_id"),
        "context_id": metadata.get("context_id"),
        "normalization": "PASS",
        "dispatch_enabled": False,
    }


def _persist_meta_shadow_event(session: Session, event: Any) -> bool:
    """Persist exact shadow lineage relationally; return False for exact replay."""

    source = "meta_whatsapp_shadow"
    payload = _shadow_event_payload(event)
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = session.scalar(
        select(InboundEventRow).where(
            InboundEventRow.tenant_id == event.tenant_id,
            InboundEventRow.source == source,
            InboundEventRow.external_event_id == event.external_event_id,
        )
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="duplicate event identity conflict",
            )
        return False
    timestamp = now_utc()
    row = InboundEventRow(
        id=(
            "metashadow_"
            + hashlib.sha256(
                f"{event.tenant_id}:{source}:{event.external_event_id}".encode()
            ).hexdigest()[:48]
        ),
        tenant_id=event.tenant_id,
        source=source,
        external_event_id=event.external_event_id,
        event_type="shadow_normalized",
        payload=payload,
        payload_hash=payload_hash,
        received_at=timestamp,
        processed_at=timestamp,
        interaction_id=None,
        status="SHADOW_NORMALIZED",
        error=None,
        correlation_id=event.correlation_id,
        lineage_classification="ORGANIC",
        scenario_run_id=None,
        scenario_step_run_id=None,
    )
    insert_conflict = False
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(InboundEventRow).where(
                InboundEventRow.tenant_id == event.tenant_id,
                InboundEventRow.source == source,
                InboundEventRow.external_event_id == event.external_event_id,
            )
        )
        if existing is not None and existing.payload_hash == payload_hash:
            return False
        insert_conflict = True
    if insert_conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="duplicate event identity conflict",
        )
    return True


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


def get_callback_session_factory():
    return SessionLocal


def create_ingress_app() -> FastAPI:
    app = FastAPI(title="Attention Router Ingress", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "live", "listener": "ingress"}

    @app.get("/api/v1/ingress/meta/whatsapp/webhook", tags=["meta-ingress"])
    def meta_verify(
        hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
        hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
        hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
        session: Session = Depends(get_session),
    ) -> Response:
        if (
            hub_mode == "subscribe"
            and settings.meta_verify_token
            and hub_verify_token
            and hmac.compare_digest(hub_verify_token, settings.meta_verify_token)
            and hub_challenge is not None
        ):
            audit(session, None, "meta_webhook_verified", {"mode": hub_mode}, origin="ingress")
            return Response(content=hub_challenge, media_type="text/plain")
        audit(session, None, "meta_webhook_verification_failed", {"mode": hub_mode}, origin="ingress")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="webhook verification failed")

    @app.post("/api/v1/ingress/meta/whatsapp/webhook", tags=["meta-ingress"])
    async def meta_webhook(
        request: Request,
        x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
        session: Session = Depends(get_session),
        callback_session_factory: Any = Depends(get_callback_session_factory),
    ) -> dict[str, Any]:
        body = await request.body()
        if len(body) > settings.meta_max_webhook_body_bytes:
            audit(session, None, "meta_webhook_body_too_large", {"bytes": len(body)}, origin="ingress")
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="request body too large")
        if not verify_meta_signature(body, x_hub_signature_256, settings.meta_app_secret):
            audit(session, None, "meta_signature_rejected", {"reason": "missing_or_invalid"}, origin="ingress")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            audit(session, None, "meta_webhook_rejected", {"reason": "invalid_json"}, origin="ingress")
            raise HTTPException(status_code=400, detail="invalid json") from exc
        status_events = list(meta_adapter.iter_statuses(payload))
        received_statuses = [(event, now_utc()) for event in status_events]
        audit(session, None, "meta_webhook_received", {"bytes": len(body)}, origin="ingress")
        processed = 0
        duplicates = 0
        conflicts = 0
        admitted = 0
        status_duplicates = 0
        scope_rejected = 0
        deferred = 0
        reconciliation_ids: set[str] = set()
        for status_event, received_at in received_statuses:
            admission_unavailable = False
            try:
                admission = admit_meta_callback_evidence(
                    callback_session_factory,
                    status_event=status_event,
                    received_at=received_at,
                    max_transaction_seconds=(
                        settings.meta_admission_transaction_timeout_seconds
                    ),
                    admission_grace_seconds=settings.meta_admission_grace_seconds,
                )
            except MetaAdmissionUnavailable:
                admission_unavailable = True
            if admission_unavailable:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="callback evidence persistence unavailable",
                ) from None
            admitted += int(admission.evidence_persisted)
            status_duplicates += int(admission.duplicate)
            scope_rejected += int(
                not admission.scope_accepted and not admission.correlation_pending
            )
            deferred += int(admission.correlation_pending)
            if admission.scope_accepted and admission.reconciliation_id:
                reconciliation_ids.add(admission.reconciliation_id)
        for reconciliation_id in sorted(reconciliation_ids):
            try:
                reconcile_meta_callback_outcome(
                    callback_session_factory,
                    reconciliation_id=reconciliation_id,
                    transaction_timeout_seconds=(
                        settings.meta_reconciliation_transaction_timeout_seconds
                    ),
                )
            except Exception as exc:
                deferred += 1
                audit(
                    session,
                    None,
                    "production_meta_reconciliation_deferred",
                    {
                        "reconciliation_id": reconciliation_id,
                        "error_type": type(exc).__name__,
                    },
                    origin="ingress",
                )
        for ignored in meta_adapter.unsupported_messages(payload):
            audit(
                session,
                None,
                "meta_event_ignored",
                {
                    "provider_message_id_hash": provider_message_id_fingerprint(
                        ignored.get("id")
                    ),
                    "type": ignored.get("type"),
                },
                origin="ingress",
            )
        try:
            events = meta_adapter.normalize_many(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            audit(
                session,
                None,
                "normalization_failed",
                {
                    "source": "meta_whatsapp",
                    "error_type": type(exc).__name__,
                },
                origin="ingress",
            )
            raise HTTPException(
                status_code=400, detail="invalid webhook payload"
            ) from None
        for event in events:
            handle_meta_control_event(session, event=event, enabled=settings.meta_human_auth_control_enabled)
        if not settings.meta_webhook_dispatch_enabled:
            duplicates = 0
            normalized = 0
            for event in events:
                if not _persist_meta_shadow_event(session, event):
                    duplicates += 1
                    continue
                metadata = event.metadata
                button_id = metadata.get("button_reply_id")
                canary = None
                if button_id:
                    for prepared in session.query(AuditEventRow).filter_by(event_type="transport_interactive_canary_prepared").all():
                        if button_id in (prepared.payload.get("button_ids") or []):
                            canary = prepared.payload
                            break
                semantic_intent = None
                if canary and button_id:
                    button_ids = canary.get("button_ids") or []
                    if button_id == button_ids[0]:
                        semantic_intent = "TEST_APPROVE"
                    elif len(button_ids) > 1 and button_id == button_ids[1]:
                        semantic_intent = "TEST_DENY"
                audit(
                    session,
                    None,
                    "meta_event_shadow_normalized",
                    {
                        "object": metadata.get("object"),
                        "field": metadata.get("field"),
                        "waba_id_hash": provider_message_id_fingerprint(
                            metadata.get("waba_id")
                        ),
                        "phone_number_id_hash": provider_message_id_fingerprint(
                            metadata.get("phone_number_id")
                        ),
                        "external_message_id_hash": provider_message_id_fingerprint(
                            event.external_event_id
                        ),
                        "sender_hash": provider_message_id_fingerprint(
                            metadata.get("sender")
                        ),
                        "message_type": metadata.get("message_type"),
                        "interactive_type": metadata.get("interactive_type"),
                        "button_reply_id_hash": provider_message_id_fingerprint(
                            button_id
                        ),
                        "context_id_hash": provider_message_id_fingerprint(
                            metadata.get("context_id")
                        ),
                        "interactive_canary_id": canary.get("authorization_id") if canary else None,
                        "interactive_canary_fingerprint": canary.get("scope_fingerprint") if canary else None,
                        "referenced_outbound_wamid_hash": provider_message_id_fingerprint(
                            metadata.get("context_id")
                        ),
                        "semantic_test_intent": semantic_intent or ("UNKNOWN" if button_id else None),
                        "normalization": "PASS",
                        "idempotency": "NEW",
                        "dispatch_enabled": False,
                    },
                    event.correlation_id,
                    origin="ingress",
                )
                normalized += 1
            audit(
                session,
                None,
                "meta_webhook_parsed_dispatch_disabled",
                {
                    "object": payload.get("object"),
                    "field": next(
                        (
                            change.get("field")
                            for entry in payload.get("entry", []) or []
                            if isinstance(entry, dict)
                            for change in entry.get("changes", []) or []
                            if isinstance(change, dict) and change.get("field")
                        ),
                        None,
                    ),
                    "message_count": len(events),
                    "normalized_count": normalized,
                    "duplicate_count": duplicates,
                    "status_count": len(status_events),
                    "dispatch_enabled": False,
                },
                origin="ingress",
            )
            result = {"status": "ok", "processed": 0, "duplicates": duplicates, "conflicts": 0, "parsed": len(events), "normalized": normalized}
            if status_events:
                result.update(
                    {
                        "status_admitted": admitted,
                        "status_duplicates": status_duplicates,
                        "status_scope_rejected": scope_rejected,
                        "reconciliation_deferred": deferred,
                    }
                )
            return result
        for event in events:
            existing = session.query(InboundEventRow).filter_by(
                source=event.source, external_event_id=event.external_event_id
            ).first()
            try:
                services.receive_normalized_inbound_event(session, event)
                audit(
                    session,
                    None,
                    "meta_event_normalized",
                    {
                        "external_event_id_hash": provider_message_id_fingerprint(
                            event.external_event_id
                        ),
                        "message_type": event.metadata.get("message_type"),
                    },
                    event.correlation_id,
                    None,
                    origin="ingress",
                )
                if existing:
                    duplicates += 1
                else:
                    processed += 1
            except services.DuplicatePayloadConflictError:
                conflicts += 1
            except Exception:
                raise
        result = {
            "status": "ok",
            "processed": processed,
            "duplicates": duplicates,
            "conflicts": conflicts,
        }
        if status_events:
            result.update(
                {
                    "status_admitted": admitted,
                    "status_duplicates": status_duplicates,
                    "status_scope_rejected": scope_rejected,
                    "reconciliation_deferred": deferred,
                }
            )
        return result

    return app


app = create_ingress_app()
