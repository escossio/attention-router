"""Temporary selection data; Client Session remains the execution authority."""

from __future__ import annotations

import secrets
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_bootstrap import (
    resolve_represented_owner_actor_key,
)
from attention_router.application.personal_context_bootstrap_product import (
    PersonalContextBootstrapProductService,
)
from attention_router.config import Settings
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import TenantRow
from attention_router.infrastructure.personal_context_bootstrap_models import (
    PersonalContextBootstrapRunRow,
)
from attention_router.infrastructure.personal_context_bootstrap_selection_models import (
    PersonalContextBootstrapSelectionRow,
)


class BootstrapSelectionError(RuntimeError):
    code = "BOOTSTRAP_SELECTION_INVALID"


class BootstrapSelectionNotFound(BootstrapSelectionError):
    code = "BOOTSTRAP_SELECTION_NOT_FOUND"


class BootstrapSelectionConflict(BootstrapSelectionError):
    code = "BOOTSTRAP_SELECTION_CONFLICT"


class BootstrapSelectionExpired(BootstrapSelectionError):
    code = "BOOTSTRAP_SELECTION_EXPIRED"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_chats(chats: list[dict]) -> tuple[list[str], list[dict]]:
    if not isinstance(chats, list) or len(chats) != 5:
        raise BootstrapSelectionError()
    indices, keys, display = [], [], []
    for chat in chats:
        if not isinstance(chat, dict) or set(chat) != {
            "index", "external_thread_key", "display_name", "thread_type"
        }:
            raise BootstrapSelectionError()
        index, key, name, kind = (
            chat["index"], chat["external_thread_key"],
            chat["display_name"], chat["thread_type"]
        )
        if (
            isinstance(index, bool) or not isinstance(index, int)
            or index < 1 or index > 5000
            or not isinstance(key, str) or not 1 <= len(key) <= 240
            or not key.strip() or key != key.strip()
            or not isinstance(name, str) or not 1 <= len(name) <= 160
            or not name.strip() or name != name.strip()
            or "@" in name or re.search(r"\d{6,}", name)
            or any(ord(character) < 32 for character in name)
            or kind not in {"DIRECT", "GROUP"}
        ):
            raise BootstrapSelectionError()
        indices.append(index)
        keys.append(key)
        display.append({"index": index, "display_name": name, "thread_type": kind})
    if len(set(indices)) != 5 or len(set(keys)) != 5:
        raise BootstrapSelectionError()
    if sum(item["thread_type"] == "GROUP" for item in display) != 1:
        raise BootstrapSelectionError()
    return keys, display


def stage_selection(
    session: Session,
    *,
    settings: Settings,
    owner_human_identity_id: str,
    chats: list[dict],
    consent_ref: str,
    processing_budget: dict[str, int],
    lifetime_hours: int = 24,
    now: datetime | None = None,
) -> PersonalContextBootstrapSelectionRow:
    """Operator stages data for the configured first-owner canary, never authority."""
    tenant_id = settings.personal_context_bootstrap_canary_tenant_id
    if (
        not settings.personal_context_bootstrap_enabled or not tenant_id
        or not isinstance(owner_human_identity_id, str) or not owner_human_identity_id
        or not isinstance(lifetime_hours, int) or not 1 <= lifetime_hours <= 72
        or not isinstance(consent_ref, str) or not 1 <= len(consent_ref) <= 240
        or not consent_ref.strip() or consent_ref != consent_ref.strip()
        or processing_budget != {
            "page_size": 50,
            "max_messages_per_chat": 1000,
            "max_total_messages": 5000,
        }
    ):
        raise BootstrapSelectionError()
    keys, display = _validate_chats(chats)
    current = now or datetime.now(UTC)
    tenant = session.scalar(
        select(TenantRow).where(TenantRow.id == tenant_id).with_for_update()
    )
    if tenant is None or tenant.status != "ACTIVE":
        raise BootstrapSelectionError()
    resolve_represented_owner_actor_key(
        session, tenant_id=tenant_id,
        owner_human_identity_id=owner_human_identity_id,
    )
    existing = session.scalar(
        select(PersonalContextBootstrapSelectionRow).where(
            PersonalContextBootstrapSelectionRow.tenant_id == tenant_id,
            PersonalContextBootstrapSelectionRow.owner_human_identity_id == owner_human_identity_id,
            PersonalContextBootstrapSelectionRow.consumed_at.is_(None),
            PersonalContextBootstrapSelectionRow.expires_at > current,
        )
    )
    if existing is not None:
        raise BootstrapSelectionConflict()
    row = PersonalContextBootstrapSelectionRow(
        id="pbs_" + secrets.token_urlsafe(24),
        tenant_id=tenant_id,
        owner_human_identity_id=owner_human_identity_id,
        chat_keys=keys,
        display_chats=display,
        expected_consent_ref=consent_ref,
        processing_budget=processing_budget,
        created_at=current,
        expires_at=current + timedelta(hours=lifetime_hours),
    )
    session.add(row)
    session.flush()
    return row


class BootstrapSelectionService:
    def __init__(self, product: PersonalContextBootstrapProductService):
        self.product = product

    def _scope(self, session: Session, session_token: str | None):
        authority = self.product._authority(session, session_token)
        resolve_represented_owner_actor_key(
            session, tenant_id=authority.active_tenant_id,
            owner_human_identity_id=authority.human_identity_id,
        )
        return authority

    def pending(self, session: Session, *, session_token: str | None):
        authority = self._scope(session, session_token)
        current = datetime.now(UTC)
        rows = list(session.scalars(select(PersonalContextBootstrapSelectionRow).where(
            PersonalContextBootstrapSelectionRow.tenant_id == authority.active_tenant_id,
            PersonalContextBootstrapSelectionRow.owner_human_identity_id == authority.human_identity_id,
            PersonalContextBootstrapSelectionRow.consumed_at.is_(None),
            PersonalContextBootstrapSelectionRow.expires_at > current,
        )).all())
        if len(rows) > 1:
            raise BootstrapSelectionConflict()
        return rows[0] if rows else None

    def confirm(
        self, session: Session, *, session_token: str | None,
        selection_id: str, consent_ref: str, processing_budget: dict[str, int],
    ) -> PersonalContextBootstrapRunRow:
        authority = self._scope(session, session_token)
        if not isinstance(selection_id, str) or not selection_id.startswith("pbs_"):
            raise BootstrapSelectionNotFound()
        row = session.scalar(select(PersonalContextBootstrapSelectionRow).where(
            PersonalContextBootstrapSelectionRow.id == selection_id,
        ).with_for_update())
        if (
            row is None or row.tenant_id != authority.active_tenant_id
            or row.owner_human_identity_id != authority.human_identity_id
        ):
            raise BootstrapSelectionNotFound()
        fingerprint = stable_hash({
            "selection_id": selection_id,
            "consent_ref": consent_ref,
            "processing_budget": processing_budget,
        })
        if row.consumed_at is not None:
            if row.confirmation_fingerprint != fingerprint or not row.bootstrap_run_id:
                raise BootstrapSelectionConflict()
            run = session.get(PersonalContextBootstrapRunRow, row.bootstrap_run_id)
            if run is None:
                raise BootstrapSelectionConflict()
            return run
        if _aware(row.expires_at) <= datetime.now(UTC):
            raise BootstrapSelectionExpired()
        if (
            consent_ref != row.expected_consent_ref
            or processing_budget != row.processing_budget
            or len(row.chat_keys) != 5 or len(set(row.chat_keys)) != 5
        ):
            raise BootstrapSelectionConflict()
        run = self.product.create_and_queue(
            session, session_token=session_token, consent_ref=consent_ref,
            chat_keys=list(row.chat_keys), processing_budget=processing_budget,
        )
        row.consumed_at = datetime.now(UTC)
        row.bootstrap_run_id = run.id
        row.confirmation_fingerprint = fingerprint
        session.flush()
        return run
