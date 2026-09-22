"""Authenticated Client Session approval inbox and decision service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionService
from attention_router.config import Settings
from attention_router.infrastructure import client_session_repository
from attention_router.infrastructure.models import (
    ExecutionIntentRow,
    HumanApprovalDecisionEvidenceRow,
    HumanExecutionAuthorizationRow,
)
from attention_router.platform.human_approval_decision import (
    HumanApprovalDecisionError,
    decide_human_approval,
    execution_intent_tenant,
)
from attention_router.platform.production_authority import ProductionAuthorityDenied
from attention_router.platform.production_bridge import (
    validate_frozen_execution_intent_authority,
)


class ClientApprovalError(Exception):
    code = "CLIENT_APPROVAL_UNAVAILABLE"


class ClientApprovalDisabled(ClientApprovalError):
    code = "CLIENT_APPROVAL_DISABLED"


class ClientApprovalNotFound(ClientApprovalError):
    code = "CLIENT_APPROVAL_NOT_FOUND"


class ClientApprovalAuthorityRejected(ClientApprovalError):
    code = "CLIENT_APPROVAL_AUTHORITY_REJECTED"


class ClientApprovalConflict(ClientApprovalError):
    code = "CLIENT_APPROVAL_CONFLICT"


class ClientApprovalExpired(ClientApprovalError):
    code = "CLIENT_APPROVAL_EXPIRED"
@dataclass(frozen=True, slots=True)
class ClientApprovalView:
    approval_id: str
    state: str
    capability: str
    operation: str
    target: str
    preview: str
    issued_at: datetime
    expires_at: datetime


class ClientApprovalService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
    ):
        self.settings = settings
        self.client_sessions = client_sessions

    def _require_enabled(self) -> None:
        if not self.settings.client_approval_enabled:
            raise ClientApprovalDisabled()

    def _authority(
        self,
        session: Session,
        session_token: str | None,
        *,
        now: datetime,
    ):
        authority = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
            now=now,
        )
        if not isinstance(session_token, str):
            raise ClientApprovalAuthorityRejected()
        session_row = client_session_repository.get_client_session_by_token(
            session,
            token=session_token,
        )
        if (
            session_row is None
            or session_row.human_identity_id != authority.human_identity_id
            or session_row.device_id != authority.device.device_id
            or session_row.tenant_id != authority.active_tenant_id
        ):
            raise ClientApprovalAuthorityRejected()
        return authority, session_row

    @staticmethod
    def _parent(session: Session, row: HumanExecutionAuthorizationRow):
        return session.get(ExecutionIntentRow, row.execution_intent_id)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    def _view(
        self,
        session: Session,
        row: HumanExecutionAuthorizationRow,
        *,
        tenant_id: str,
    ) -> ClientApprovalView:
        parent = self._parent(session, row)
        if (
            parent is None
            or parent.scope_fingerprint != row.execution_intent_fingerprint
            or execution_intent_tenant(parent) != tenant_id
        ):
            raise ClientApprovalAuthorityRejected()
        scope = parent.scope if isinstance(parent.scope, dict) else {}
        frozen = scope.get("frozen_authority")
        target = scope.get("target")
        immutable = scope.get("immutable_inputs")
        if not isinstance(frozen, dict) or not isinstance(target, dict):
            raise ClientApprovalAuthorityRejected()
        if not isinstance(immutable, dict):
            immutable = {}
        capability = frozen.get("capability")
        operation = frozen.get("operation")
        canonical_target = target.get("canonical_address")
        preview = immutable.get("effective_response_snapshot")
        if not all(
            isinstance(value, str) and value
            for value in (capability, operation, canonical_target, preview)
        ):
            raise ClientApprovalAuthorityRejected()
        return ClientApprovalView(
            approval_id=row.id,
            state=row.state,
            capability=capability[:120],
            operation=operation[:120],
            target=canonical_target[:180],
            preview=preview[:4000],
            issued_at=self._aware(row.issued_at),
            expires_at=self._aware(row.expires_at),
        )

    def list_pending(
        self,
        session: Session,
        *,
        session_token: str | None,
        now: datetime | None = None,
    ) -> tuple[ClientApprovalView, ...]:
        self._require_enabled()
        current = now or datetime.now(UTC)
        authority, _session_row = self._authority(
            session,
            session_token,
            now=current,
        )
        rows = list(
            session.scalars(
                select(HumanExecutionAuthorizationRow)
                .where(
                    HumanExecutionAuthorizationRow.approval_channel
                    == "android_client",
                    HumanExecutionAuthorizationRow.expected_approver
                    == authority.human_identity_id,
                    HumanExecutionAuthorizationRow.state
                    == "PENDING_HUMAN_APPROVAL",
                    HumanExecutionAuthorizationRow.expires_at > current,
                )
                .order_by(
                    HumanExecutionAuthorizationRow.issued_at,
                    HumanExecutionAuthorizationRow.id,
                )
                .limit(50)
            ).all()
        )
        views: list[ClientApprovalView] = []
        for row in rows:
            try:
                view = self._view(
                    session,
                    row,
                    tenant_id=authority.active_tenant_id,
                )
            except ClientApprovalAuthorityRejected:
                continue
            views.append(view)
        return tuple(views)

    def get(
        self,
        session: Session,
        *,
        session_token: str | None,
        approval_id: str,
        now: datetime | None = None,
    ) -> ClientApprovalView:
        self._require_enabled()
        current = now or datetime.now(UTC)
        authority, _session_row = self._authority(
            session,
            session_token,
            now=current,
        )
        row = session.get(HumanExecutionAuthorizationRow, approval_id)
        if (
            row is None
            or row.approval_channel != "android_client"
            or row.expected_approver != authority.human_identity_id
            or row.state == "PREPARED"
        ):
            raise ClientApprovalNotFound()
        if self._aware(row.expires_at) <= current:
            raise ClientApprovalExpired()
        return self._view(
            session,
            row,
            tenant_id=authority.active_tenant_id,
        )

    def decide(
        self,
        session: Session,
        *,
        session_token: str | None,
        approval_id: str,
        decision: str,
        now: datetime | None = None,
    ) -> ClientApprovalView:
        self._require_enabled()
        current = now or datetime.now(UTC)
        authority, session_row = self._authority(
            session,
            session_token,
            now=current,
        )
        row = session.get(HumanExecutionAuthorizationRow, approval_id)
        if (
            row is None
            or row.approval_channel != "android_client"
            or row.expected_approver != authority.human_identity_id
        ):
            raise ClientApprovalNotFound()
        parent = self._parent(session, row)
        if parent is None:
            raise ClientApprovalNotFound()
        if execution_intent_tenant(parent) != authority.active_tenant_id:
            raise ClientApprovalAuthorityRejected()

        if row.state in {"APPROVED", "DENIED"}:
            evidence = session.scalar(
                select(HumanApprovalDecisionEvidenceRow).where(
                    HumanApprovalDecisionEvidenceRow.authorization_id
                    == row.id
                )
            )
            expected_state = (
                "APPROVED" if decision == "APPROVE" else "DENIED"
            )
            if (
                evidence is not None
                and evidence.channel == "ANDROID_CLIENT"
                and evidence.approver_reference
                == authority.human_identity_id
                and evidence.human_identity_id
                == authority.human_identity_id
                and evidence.decision == decision
                and row.state == expected_state
            ):
                return self._view(
                    session,
                    row,
                    tenant_id=authority.active_tenant_id,
                )
            raise ClientApprovalConflict()

        if parent.authority_profile_id is not None:
            try:
                validate_frozen_execution_intent_authority(
                    session,
                    parent=parent,
                    expected_fingerprint=row.execution_intent_fingerprint,
                    effective_response_snapshot=parent.scope[
                        "immutable_inputs"
                    ]["effective_response_snapshot"],
                    now=current,
                )
            except (KeyError, ProductionAuthorityDenied) as exc:
                raise ClientApprovalAuthorityRejected() from exc
        reference = hashlib.sha256(
            (
                f"{row.id}|{authority.human_identity_id}|"
                f"{authority.device.device_id}|{decision}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            decide_human_approval(
                session,
                authorization_id=row.id,
                decision=decision,
                channel="ANDROID_CLIENT",
                approver_reference=authority.human_identity_id,
                tenant_id=authority.active_tenant_id,
                decision_reference=reference,
                human_identity_id=authority.human_identity_id,
                device_id=authority.device.device_id,
                client_session_id=session_row.id,
                now=current,
            )
        except HumanApprovalDecisionError as exc:
            reason = str(exc)
            if reason == "HUMAN_AUTH_EXPIRED_OR_NOT_PENDING":
                if row.state in {"APPROVED", "DENIED"}:
                    pass
                else:
                    raise ClientApprovalExpired() from exc
            elif reason == "HUMAN_AUTH_ALREADY_DECIDED_OR_MISSING":
                raise ClientApprovalConflict() from exc
            elif reason in {
                "HUMAN_AUTH_IDENTITY_OR_CONTEXT_MISMATCH",
                "HUMAN_AUTH_TENANT_MISMATCH",
                "EXECUTION_INTENT_FINGERPRINT_MISMATCH",
                "EXECUTION_INTENT_NOT_FROZEN",
            }:
                raise ClientApprovalAuthorityRejected() from exc
            else:
                raise ClientApprovalConflict() from exc
        session.flush()
        return self._view(
            session,
            row,
            tenant_id=authority.active_tenant_id,
        )


__all__ = [
    "ClientApprovalAuthorityRejected",
    "ClientApprovalConflict",
    "ClientApprovalDisabled",
    "ClientApprovalError",
    "ClientApprovalExpired",
    "ClientApprovalNotFound",
    "ClientApprovalService",
    "ClientApprovalView",
]
