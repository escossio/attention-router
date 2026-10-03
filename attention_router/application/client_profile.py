"""Authenticated owner profile used by Andy to refer to the represented human."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionService
from attention_router.config import Settings
from attention_router.core.client.bootstrap import TenantRole
from attention_router.infrastructure.human_identity_models import HumanProfileRow


class ClientProfileError(Exception):
    code = "CLIENT_PROFILE_UNAVAILABLE"


class ClientProfileDisabled(ClientProfileError):
    code = "CLIENT_PROFILE_DISABLED"


class ClientProfileAuthorityRejected(ClientProfileError):
    code = "CLIENT_PROFILE_AUTHORITY_REJECTED"


@dataclass(frozen=True, slots=True)
class ClientProfileView:
    assistant_reference_name: str | None
    updated_at: datetime | None


class ClientProfileService:
    def __init__(self, *, settings: Settings, client_sessions: ClientSessionService):
        self.settings = settings
        self.client_sessions = client_sessions

    def _require_enabled(self) -> None:
        if not self.settings.client_profile_enabled:
            raise ClientProfileDisabled()

    def _owner_authority(self, session: Session, session_token: str | None, *, now: datetime):
        authority = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
            now=now,
        )
        membership = next(
            (
                item
                for item in authority.memberships
                if item.tenant_id == authority.active_tenant_id
            ),
            None,
        )
        if membership is None or membership.role != TenantRole.OWNER:
            raise ClientProfileAuthorityRejected()
        return authority

    @staticmethod
    def _normalized_name(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or len(normalized) > 160:
            raise ValueError("CLIENT_PROFILE_REFERENCE_NAME_INVALID")
        return normalized

    def get(self, session: Session, *, session_token: str | None, now: datetime | None = None) -> ClientProfileView:
        self._require_enabled()
        current = now or datetime.now(UTC)
        authority = self._owner_authority(session, session_token, now=current)
        row = session.get(HumanProfileRow, authority.human_identity_id)
        return ClientProfileView(
            assistant_reference_name=row.assistant_reference_name if row is not None else None,
            updated_at=row.updated_at if row is not None else None,
        )

    def update(
        self,
        session: Session,
        *,
        session_token: str | None,
        assistant_reference_name: str | None,
        now: datetime | None = None,
    ) -> ClientProfileView:
        self._require_enabled()
        current = now or datetime.now(UTC)
        authority = self._owner_authority(session, session_token, now=current)
        normalized = self._normalized_name(assistant_reference_name)
        row = session.get(HumanProfileRow, authority.human_identity_id)
        if row is None:
            row = HumanProfileRow(
                human_identity_id=authority.human_identity_id,
                assistant_reference_name=normalized,
                created_at=current,
                updated_at=current,
            )
            session.add(row)
        else:
            row.assistant_reference_name = normalized
            row.updated_at = current
        session.flush()
        return ClientProfileView(
            assistant_reference_name=row.assistant_reference_name,
            updated_at=row.updated_at,
        )


__all__ = [
    "ClientProfileAuthorityRejected",
    "ClientProfileDisabled",
    "ClientProfileError",
    "ClientProfileService",
    "ClientProfileView",
]
