"""Authenticated provider-neutral Andy command channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
import unicodedata
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from attention_router.adapters.wwebjs_owner_control import (
    OwnerControlParseResult,
    OwnerControlParseStatus,
    parse_owner_grace_control,
    render_owner_control_confirmation,
    render_owner_control_error,
)
from attention_router.application.client_session import ClientSessionService
from attention_router.application.owner_control import (
    AutomaticResponsesEnabledParameters,
    OwnerControlAction,
    OwnerControlError,
    dispatch_owner_control_action,
)
from attention_router.application.owner_control_semantic import (
    OwnerControlSemanticError,
    interpret_owner_control_semantically,
)
from attention_router.application.owner_operational_control import (
    OperationalControlConflict,
    OperationalControlError,
    OperationalControlUnauthorized,
)
from attention_router.application.platform.context import resolve_represented_subject
from attention_router.config import Settings
from attention_router.core.client.bootstrap import TenantRole
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.infrastructure import client_session_repository
from attention_router.infrastructure.models import ClientCommandMessageRow
from attention_router.infrastructure.repository import audit


CLIENT_COMMAND_SOURCE_CHANNEL = "android-client-command"


class ClientCommandError(Exception):
    code = "CLIENT_COMMAND_UNAVAILABLE"


class ClientCommandDisabled(ClientCommandError):
    code = "CLIENT_COMMAND_DISABLED"


class ClientCommandAuthorityRejected(ClientCommandError):
    code = "CLIENT_COMMAND_AUTHORITY_REJECTED"


class ClientCommandConflict(ClientCommandError):
    code = "CLIENT_COMMAND_CONFLICT"


class ClientCommandInvalid(ClientCommandError):
    code = "CLIENT_COMMAND_INVALID"


@dataclass(frozen=True, slots=True)
class ClientCommandView:
    command_id: str
    client_request_id: str
    modality: str
    input_text: str
    state: str
    normalized_action: str | None
    response_text: str | None
    error_code: str | None
    created_at: datetime
    processed_at: datetime | None
def _normalized_phrase(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().strip()
    value = re.sub(r"^andy\s*[,;:]?\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.rstrip(".!?").strip()


def _client_fast_path(text: str) -> OwnerControlParseResult | None:
    normalized = _normalized_phrase(text)
    if normalized in {
        "pare",
        "parar",
        "pare agora",
        "pare por enquanto",
        "pausa",
        "pause",
    }:
        return OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=False),
        )
    if normalized in {
        "retome",
        "retoma",
        "retomar",
        "volte",
        "volte a responder",
    }:
        return OwnerControlParseResult(
            OwnerControlParseStatus.MATCHED,
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED,
            AutomaticResponsesEnabledParameters(enabled=True),
        )
    return None


class ClientCommandService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
    ):
        self.settings = settings
        self.client_sessions = client_sessions

    def _require_enabled(self) -> None:
        if not self.settings.client_command_enabled:
            raise ClientCommandDisabled()

    def _authority(
        self,
        session: Session,
        session_token: str | None,
        *,
        now: datetime,
    ):
        bootstrap = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
            now=now,
        )
        if not isinstance(session_token, str):
            raise ClientCommandAuthorityRejected()
        session_row = client_session_repository.get_client_session_by_token(
            session,
            token=session_token,
        )
        if (
            session_row is None
            or session_row.human_identity_id != bootstrap.human_identity_id
            or session_row.device_id != bootstrap.device.device_id
            or session_row.tenant_id != bootstrap.active_tenant_id
        ):
            raise ClientCommandAuthorityRejected()
        active_membership = next(
            (
                item
                for item in bootstrap.memberships
                if item.tenant_id == bootstrap.active_tenant_id
            ),
            None,
        )
        if (
            active_membership is None
            or active_membership.role != TenantRole.OWNER
        ):
            raise ClientCommandAuthorityRejected()

        operational_scopes: list[tuple[str, str]] = []
        for membership in bootstrap.memberships:
            if membership.role != TenantRole.OWNER:
                continue
            represented = resolve_represented_subject(
                session,
                membership.tenant_id,
            )
            if represented is not None:
                operational_scopes.append(
                    (membership.tenant_id, represented.entity_id)
                )
        if len(operational_scopes) != 1:
            raise ClientCommandAuthorityRejected()

        operational_tenant_id, owner_actor_key = operational_scopes[0]
        operator = OperatorAuthority(
            tenant_id=operational_tenant_id,
            operator_actor_id=owner_actor_key,
            authenticated=True,
            roles=["OWNER"],
        )
        return (
            bootstrap,
            session_row,
            operational_tenant_id,
            owner_actor_key,
            operator,
        )
    @staticmethod
    def _view(row: ClientCommandMessageRow) -> ClientCommandView:
        return ClientCommandView(
            command_id=row.id,
            client_request_id=row.client_request_id,
            modality=row.modality,
            input_text=row.input_text,
            state=row.state,
            normalized_action=row.normalized_action,
            response_text=row.response_text,
            error_code=row.error_code,
            created_at=row.created_at,
            processed_at=row.processed_at,
        )

    def _existing(
        self,
        session: Session,
        *,
        tenant_id: str,
        human_identity_id: str,
        client_request_id: str,
    ) -> ClientCommandMessageRow | None:
        return session.scalar(
            select(ClientCommandMessageRow).where(
                ClientCommandMessageRow.tenant_id == tenant_id,
                ClientCommandMessageRow.human_identity_id
                == human_identity_id,
                ClientCommandMessageRow.client_request_id
                == client_request_id,
            )
        )

    def _interpret(self, text: str) -> OwnerControlParseResult:
        fast = _client_fast_path(text)
        if fast is not None:
            return fast
        parsed = parse_owner_grace_control(text)
        if parsed.status != OwnerControlParseStatus.NOT_CONTROL_COMMAND:
            return parsed
        try:
            semantic = interpret_owner_control_semantically(text)
        except OwnerControlSemanticError:
            return parsed
        return semantic

    def submit_text(
        self,
        session: Session,
        *,
        session_token: str | None,
        client_request_id: str,
        text: str,
        now: datetime | None = None,
    ) -> ClientCommandView:
        self._require_enabled()
        current = now or datetime.now(UTC)
        stripped = text.strip() if isinstance(text, str) else ""
        if (
            not client_request_id
            or len(client_request_id) > 80
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", client_request_id)
            or not stripped
            or len(stripped) > 4000
        ):
            raise ClientCommandInvalid()

        (
            bootstrap,
            session_row,
            operational_tenant_id,
            owner_actor_key,
            operator,
        ) = self._authority(
            session,
            session_token,
            now=current,
        )
        existing = self._existing(
            session,
            tenant_id=bootstrap.active_tenant_id,
            human_identity_id=bootstrap.human_identity_id,
            client_request_id=client_request_id,
        )
        if existing is not None:
            if existing.modality == "TEXT" and existing.input_text == stripped:
                return self._view(existing)
            raise ClientCommandConflict()

        row = ClientCommandMessageRow(
            id=str(uuid4()),
            tenant_id=bootstrap.active_tenant_id,
            human_identity_id=bootstrap.human_identity_id,
            device_id=bootstrap.device.device_id,
            client_session_id=session_row.id,
            client_request_id=client_request_id,
            modality="TEXT",
            input_text=stripped,
            state="RECEIVED",
            normalized_action=None,
            response_text=None,
            error_code=None,
            created_at=current,
            processed_at=None,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            replay = self._existing(
                session,
                tenant_id=bootstrap.active_tenant_id,
                human_identity_id=bootstrap.human_identity_id,
                client_request_id=client_request_id,
            )
            if (
                replay is not None
                and replay.modality == "TEXT"
                and replay.input_text == stripped
            ):
                return self._view(replay)
            raise ClientCommandConflict() from exc

        audit(
            session,
            None,
            "client_command.received",
            {
                "command_id": row.id,
                "modality": row.modality,
                "device_id": row.device_id,
            },
            correlation_id=row.id,
            causation_id=row.id,
            origin="client_command",
            tenant_id=row.tenant_id,
            created_at=current,
        )

        parsed = self._interpret(stripped)
        if parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND:
            row.state = "GENERAL_TASK_PENDING"
            row.response_text = (
                "Comando recebido pela Andy. Esta solicitação seguirá pelo "
                "fluxo geral de tarefas."
            )
            row.processed_at = current
            audit(
                session,
                None,
                "client_command.general_task_pending",
                {"command_id": row.id},
                correlation_id=row.id,
                causation_id=row.id,
                origin="client_command",
                tenant_id=row.tenant_id,
                created_at=current,
            )
            session.flush()
            return self._view(row)

        if parsed.status == OwnerControlParseStatus.REJECTED:
            reason = parsed.reason_code or "CONTROL_COMMAND_INVALID_VALUE"
            row.state = (
                "CLARIFICATION_REQUIRED"
                if reason in {
                    "CONTROL_COMMAND_AMBIGUOUS",
                    "CONTROL_COMMAND_NEEDS_CLARIFICATION",
                }
                else "FAILED"
            )
            row.error_code = reason
            row.response_text = render_owner_control_error(reason)
            row.processed_at = current
            session.flush()
            return self._view(row)

        if parsed.action is None or parsed.parameters is None:
            row.state = "FAILED"
            row.error_code = "OWNER_CONTROL_NORMALIZATION_INVALID"
            row.response_text = render_owner_control_error(row.error_code)
            row.processed_at = current
            session.flush()
            return self._view(row)

        row.normalized_action = parsed.action.value
        try:
            result = dispatch_owner_control_action(
                session,
                tenant_id=operational_tenant_id,
                owner_actor_key=owner_actor_key,
                action=parsed.action,
                parameters=parsed.parameters,
                authority=operator,
                source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
                source_event_id=row.id,
                command_id=row.id,
                provenance={
                    "command_id": row.id,
                    "human_identity_id": row.human_identity_id,
                    "device_id": row.device_id,
                    "client_session_id": row.client_session_id,
                    "source_client_tenant_id": row.tenant_id,
                    "operational_tenant_id": operational_tenant_id,
                    "authentication_mechanism": "CLIENT_SESSION_OWNER",
                },
            )
        except (
            OwnerControlError,
            OwnerCommandUnauthorized,
            OperationalControlConflict,
            OperationalControlError,
            OperationalControlUnauthorized,
            ValueError,
        ) as exc:
            row.state = "FAILED"
            row.error_code = str(exc)[:120]
            row.response_text = render_owner_control_error(row.error_code)
        else:
            row.state = "COMPLETED"
            row.response_text = render_owner_control_confirmation(
                result,
                action=parsed.action,
            )
        row.processed_at = current
        audit(
            session,
            None,
            "client_command.processed",
            {
                "command_id": row.id,
                "state": row.state,
                "normalized_action": row.normalized_action,
                "error_code": row.error_code,
            },
            correlation_id=row.id,
            causation_id=row.id,
            origin="client_command",
            tenant_id=row.tenant_id,
            created_at=current,
        )
        session.flush()
        return self._view(row)
    def list_recent(
        self,
        session: Session,
        *,
        session_token: str | None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[ClientCommandView, ...]:
        self._require_enabled()
        current = now or datetime.now(UTC)
        (
            bootstrap,
            _session_row,
            _operational_tenant_id,
            _owner_actor_key,
            _operator,
        ) = self._authority(
            session,
            session_token,
            now=current,
        )
        bounded = max(1, min(limit, 100))
        rows = list(
            session.scalars(
                select(ClientCommandMessageRow)
                .where(
                    ClientCommandMessageRow.tenant_id
                    == bootstrap.active_tenant_id,
                    ClientCommandMessageRow.human_identity_id
                    == bootstrap.human_identity_id,
                )
                .order_by(
                    ClientCommandMessageRow.created_at.desc(),
                    ClientCommandMessageRow.id.desc(),
                )
                .limit(bounded)
            ).all()
        )
        rows.reverse()
        return tuple(self._view(row) for row in rows)


__all__ = [
    "CLIENT_COMMAND_SOURCE_CHANNEL",
    "ClientCommandAuthorityRejected",
    "ClientCommandConflict",
    "ClientCommandDisabled",
    "ClientCommandError",
    "ClientCommandInvalid",
    "ClientCommandService",
    "ClientCommandView",
]
