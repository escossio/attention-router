"""Authenticated product service for Personal Context Semantic Bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionService
from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
    create_bootstrap_run,
    queue_bootstrap_run,
    request_bootstrap_control,
    resolve_bootstrap_owner_actor_key,
    resume_bootstrap_run,
)
from attention_router.config import Settings
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)


@dataclass(frozen=True, slots=True)
class BootstrapProductView:
    run_id: str
    state: str
    source_kind: str
    source_account: str
    progress: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    paused_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    failed_at: datetime | None
    failure_summary: str | None


class PersonalContextBootstrapProductService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
    ) -> None:
        self.settings = settings
        self.client_sessions = client_sessions

    @staticmethod
    def _view(row: PersonalContextBootstrapRunRow) -> BootstrapProductView:
        return BootstrapProductView(
            run_id=row.id,
            state=row.state,
            source_kind=row.source_kind,
            source_account=row.source_account,
            progress=dict(row.progress or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            paused_at=row.paused_at,
            completed_at=row.completed_at,
            cancelled_at=row.cancelled_at,
            failed_at=row.failed_at,
            failure_summary=row.failure_summary,
        )

    def _authority(self, session: Session, session_token: str | None):
        authority = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
        )
        actor_key = resolve_bootstrap_owner_actor_key(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
        )
        return authority, actor_key

    @staticmethod
    def _owned_run(
        session: Session,
        *,
        run_id: str,
        tenant_id: str,
        human_identity_id: str,
    ) -> PersonalContextBootstrapRunRow:
        row = session.get(PersonalContextBootstrapRunRow, run_id)
        if (
            row is None
            or row.tenant_id != tenant_id
            or row.owner_human_identity_id != human_identity_id
        ):
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_RUN_NOT_FOUND"
            )
        return row

    def start(
        self,
        session: Session,
        *,
        session_token: str | None,
        client_request_id: str,
        chat_keys: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> BootstrapProductView:
        if (
            not isinstance(client_request_id, str)
            or not client_request_id
            or len(client_request_id) > 80
        ):
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_CLIENT_REQUEST_ID_INVALID"
            )
        if len(chat_keys) > 100 or any(
            not isinstance(key, str)
            or not key
            or len(key) > 240
            for key in chat_keys
        ):
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_CHAT_SELECTION_INVALID"
            )
        if len(set(chat_keys)) != len(chat_keys):
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_CHAT_SELECTION_INVALID"
            )

        authority, actor_key = self._authority(session, session_token)
        consent_ref = (
            "client-bootstrap-consent:"
            + stable_hash(
                {
                    "human_identity_id": authority.human_identity_id,
                    "tenant_id": authority.active_tenant_id,
                    "device_id": authority.device.device_id,
                    "client_request_id": client_request_id,
                    "source_kind": "WHATSAPP_TEXT",
                }
            )[:96]
        )
        row, _created = create_bootstrap_run(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
            represented_owner_actor_key=actor_key,
            source_kind="WHATSAPP_TEXT",
            source_account=self.settings.whatsapp_history_source_account,
            consent_ref=consent_ref,
            source_selection=(
                {"chat_keys": list(chat_keys)}
                if chat_keys
                else {}
            ),
            now=now,
        )
        if row.state == "CREATED":
            row = queue_bootstrap_run(session, row.id, now=now)
        return self._view(row)

    def status(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> BootstrapProductView:
        authority, _actor_key = self._authority(session, session_token)
        row = self._owned_run(
            session,
            run_id=run_id,
            tenant_id=authority.active_tenant_id,
            human_identity_id=authority.human_identity_id,
        )
        return self._view(row)

    def list_recent(
        self,
        session: Session,
        *,
        session_token: str | None,
        limit: int = 20,
    ) -> tuple[BootstrapProductView, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("BOOTSTRAP_LIST_LIMIT_INVALID")
        authority, _actor_key = self._authority(session, session_token)
        rows = list(
            session.scalars(
                select(PersonalContextBootstrapRunRow)
                .where(
                    PersonalContextBootstrapRunRow.tenant_id
                    == authority.active_tenant_id,
                    PersonalContextBootstrapRunRow.owner_human_identity_id
                    == authority.human_identity_id,
                )
                .order_by(
                    PersonalContextBootstrapRunRow.created_at.desc(),
                    PersonalContextBootstrapRunRow.id,
                )
                .limit(limit)
            ).all()
        )
        return tuple(self._view(row) for row in rows)

    def control(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
        action: str,
        now: datetime | None = None,
    ) -> BootstrapProductView:
        authority, _actor_key = self._authority(session, session_token)
        row = self._owned_run(
            session,
            run_id=run_id,
            tenant_id=authority.active_tenant_id,
            human_identity_id=authority.human_identity_id,
        )
        normalized = action.strip().upper()
        if normalized == "RESUME":
            row = resume_bootstrap_run(session, row.id, now=now)
        elif normalized in {"PAUSE", "CANCEL"}:
            row = request_bootstrap_control(
                session,
                row.id,
                control=normalized,
                now=now,
            )
        else:
            raise PersonalContextBootstrapError(
                "BOOTSTRAP_CONTROL_UNSUPPORTED"
            )
        return self._view(row)


__all__ = [
    "BootstrapProductView",
    "PersonalContextBootstrapProductService",
]
