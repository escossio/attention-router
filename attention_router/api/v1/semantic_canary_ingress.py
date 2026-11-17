"""Optional routes in the EXISTING internal listener; absent by default."""
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from attention_router.adapters.internal_ingress import InternalIngressAdapter
from attention_router.application.voice_media import MediaReadyNotification, record_media_notification
from attention_router.config import settings
from attention_router.web.internal_security import verify_internal_signature


def build_semantic_canary_ingress_router(*, get_session, service, clock=lambda: datetime.now(UTC)):
    router = APIRouter(prefix='/api/v1/ingress/internal/semantic-canary')

    async def payload(request):
        body = await request.body()
        if len(body) > settings.internal_ingress_max_body_bytes:
            raise HTTPException(413, 'request body too large')
        if not verify_internal_signature(body, request.headers.get('X-Attention-Timestamp'),
                                         request.headers.get('X-Attention-Signature'))[0]:
            raise HTTPException(401, 'invalid internal signature')
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not isinstance(value.get('tenant_id'), str) or not value['tenant_id'].strip():
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise HTTPException(422, 'invalid canary payload') from None

    @router.post('/events', status_code=202)
    async def events(request: Request, session=Depends(get_session)):
        value = await payload(request)
        try:
            event = InternalIngressAdapter().normalize(value)
            return service.observe_trusted(session, event=event, now=clock())
        except (ValueError, TypeError):
            session.rollback()
            # Terminal denial consumes the canary-only spool record. It MUST
            # never retry against ordinary ingress or authorize a download.
            return dict(status='canary_blocked', canary_media_allowed=False, grants_authority=False)

    @router.post('/media', status_code=202)
    async def media(request: Request, session=Depends(get_session)):
        value = await payload(request)
        try:
            notification = MediaReadyNotification.model_validate(value)
            # This endpoint must never adopt a media record from the old path.
            from sqlalchemy import select
            from attention_router.infrastructure.models import ConversationMessageRow, InboundEventRow
            event = session.scalar(select(InboundEventRow).where(
                InboundEventRow.tenant_id == notification.tenant_id,
                InboundEventRow.source == notification.source,
                InboundEventRow.external_event_id == notification.external_event_id))
            row = session.scalar(select(ConversationMessageRow).where(
                ConversationMessageRow.tenant_id == notification.tenant_id,
                ConversationMessageRow.metadata_json['source_inbound_event_id'].as_string() == (event.id if event else '')))
            if row is None or not (row.metadata_json or {}).get('semantic_canary_id'):
                raise ValueError()
            record_media_notification(session, notification,
                canary_guard=lambda db, event_id: service.processing_guard(db, event_id=event_id, now=clock()))
            return dict(status='accepted', grants_authority=False)
        except (ValueError, TypeError):
            session.rollback()
            return dict(status='canary_blocked', grants_authority=False)

    return router
