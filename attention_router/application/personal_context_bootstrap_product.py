"""Authenticated product boundary for Semantic Bootstrap Product Runtime V0."""

from __future__ import annotations

from sqlalchemy.orm import Session

from attention_router.application.client_session import ClientSessionService
from attention_router.application.personal_context_bootstrap import (
    PersonalContextBootstrapError,
    create_bootstrap_run,
    queue_bootstrap_run,
    request_bootstrap_control,
    resolve_represented_owner_actor_key,
    resume_bootstrap_run,
)
from attention_router.config import Settings
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.integrations.whatsapp_history import WhatsAppHistoryAdapter


class PersonalContextBootstrapProductError(RuntimeError):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_UNAVAILABLE"


class PersonalContextBootstrapProductDisabled(
    PersonalContextBootstrapProductError
):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_DISABLED"


class PersonalContextBootstrapProductTenantForbidden(
    PersonalContextBootstrapProductError
):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_TENANT_FORBIDDEN"


class PersonalContextBootstrapProductRunNotFound(
    PersonalContextBootstrapProductError
):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_RUN_NOT_FOUND"


class PersonalContextBootstrapProductSelectionInvalid(
    PersonalContextBootstrapProductError
):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_SELECTION_INVALID"


class PersonalContextBootstrapProductBudgetInvalid(
    PersonalContextBootstrapProductError
):
    code = "PERSONAL_CONTEXT_BOOTSTRAP_BUDGET_INVALID"


class PersonalContextBootstrapProductService:
    """Owner-authenticated product facade over the already-merged V2B lifecycle."""

    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
        adapter: WhatsAppHistoryAdapter,
    ):
        self.settings = settings
        self.client_sessions = client_sessions
        self.adapter = adapter

    def _require_enabled(self) -> None:
        if not self.settings.personal_context_bootstrap_enabled:
            raise PersonalContextBootstrapProductDisabled()

    def _authority(self, session: Session, session_token: str | None):
        self._require_enabled()
        authority = self.client_sessions.authenticated_bootstrap(
            session,
            session_token=session_token,
        )
        canary = self.settings.personal_context_bootstrap_canary_tenant_id
        if canary is not None and authority.active_tenant_id != canary:
            raise PersonalContextBootstrapProductTenantForbidden()
        return authority

    def _run_for_authority(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunRow:
        authority = self._authority(session, session_token)
        row = session.get(PersonalContextBootstrapRunRow, run_id)
        if (
            row is None
            or row.tenant_id != authority.active_tenant_id
            or row.owner_human_identity_id != authority.human_identity_id
        ):
            raise PersonalContextBootstrapProductRunNotFound()
        return row

    def _selection(self, chat_keys: list[str] | None) -> list[str]:
        chats = self.adapter.list_chats()
        available = {
            item["external_thread_key"]
            for item in chats
        }
        if chat_keys is None:
            selected = sorted(available)
        else:
            if (
                not isinstance(chat_keys, list)
                or not chat_keys
                or len(chat_keys) > 5000
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or len(item.strip()) > 240
                    for item in chat_keys
                )
            ):
                raise PersonalContextBootstrapProductSelectionInvalid()
            selected = sorted({item.strip() for item in chat_keys})
            if any(item not in available for item in selected):
                raise PersonalContextBootstrapProductSelectionInvalid()
        if not selected:
            raise PersonalContextBootstrapProductSelectionInvalid()
        return selected

    def _budget(
        self,
        *,
        selected_chat_count: int,
        processing_budget: dict[str, int] | None,
    ) -> dict[str, int]:
        snapshot_limit = self.adapter.snapshot_limit
        budget = {
            "page_size": min(50, snapshot_limit),
            "max_messages_per_chat": snapshot_limit,
            "max_total_messages": min(
                500,
                max(1, selected_chat_count) * snapshot_limit,
            ),
        }
        if processing_budget is not None:
            if not isinstance(processing_budget, dict):
                raise PersonalContextBootstrapProductBudgetInvalid()
            unknown = set(processing_budget) - set(budget)
            if unknown:
                raise PersonalContextBootstrapProductBudgetInvalid()
            budget.update(processing_budget)
        if (
            isinstance(budget["page_size"], bool)
            or not isinstance(budget["page_size"], int)
            or not 1 <= budget["page_size"] <= snapshot_limit
            or isinstance(budget["max_messages_per_chat"], bool)
            or not isinstance(budget["max_messages_per_chat"], int)
            or not 1 <= budget["max_messages_per_chat"] <= snapshot_limit
            or isinstance(budget["max_total_messages"], bool)
            or not isinstance(budget["max_total_messages"], int)
            or not 1 <= budget["max_total_messages"] <= 5000
        ):
            raise PersonalContextBootstrapProductBudgetInvalid()
        return budget

    def create_and_queue(
        self,
        session: Session,
        *,
        session_token: str | None,
        consent_ref: str,
        chat_keys: list[str] | None = None,
        processing_budget: dict[str, int] | None = None,
    ) -> PersonalContextBootstrapRunRow:
        authority = self._authority(session, session_token)
        selected = self._selection(chat_keys)
        budget = self._budget(
            selected_chat_count=len(selected),
            processing_budget=processing_budget,
        )
        owner_actor_key = resolve_represented_owner_actor_key(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
        )
        row, _created = create_bootstrap_run(
            session,
            tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
            represented_owner_actor_key=owner_actor_key,
            source_kind="WHATSAPP_TEXT",
            source_account=self.settings.local_source_account,
            source_revision="wwebjs-limit-only-snapshot-v0",
            source_selection={"chat_keys": selected},
            consent_ref=consent_ref,
            processing_budget=budget,
        )
        if row.state == "CREATED":
            queue_bootstrap_run(session, row.id)
        return row

    def status(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunRow:
        return self._run_for_authority(
            session,
            session_token=session_token,
            run_id=run_id,
        )

    def pause(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunRow:
        row = self._run_for_authority(
            session,
            session_token=session_token,
            run_id=run_id,
        )
        return request_bootstrap_control(
            session,
            row.id,
            control="PAUSE",
        )

    def resume(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunRow:
        row = self._run_for_authority(
            session,
            session_token=session_token,
            run_id=run_id,
        )
        return resume_bootstrap_run(session, row.id)

    def cancel(
        self,
        session: Session,
        *,
        session_token: str | None,
        run_id: str,
    ) -> PersonalContextBootstrapRunRow:
        row = self._run_for_authority(
            session,
            session_token=session_token,
            run_id=run_id,
        )
        return request_bootstrap_control(
            session,
            row.id,
            control="CANCEL",
        )


__all__ = [
    "PersonalContextBootstrapError",
    "PersonalContextBootstrapProductBudgetInvalid",
    "PersonalContextBootstrapProductDisabled",
    "PersonalContextBootstrapProductError",
    "PersonalContextBootstrapProductRunNotFound",
    "PersonalContextBootstrapProductSelectionInvalid",
    "PersonalContextBootstrapProductService",
    "PersonalContextBootstrapProductTenantForbidden",
]
