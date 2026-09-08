"""Internal Capability Pack V1 providers and durable scheduler.

All domain behavior is selected through the already materialized provider
binding.  The worker only invokes ``fire_due_reminders`` as a generic durable
scheduled-event pump; it contains no capability-name routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from attention_router.application.platform.authority import (
    create_capability_grant,
    revoke_capability_grant,
)
from attention_router.application.platform.entities import set_entity_state
from attention_router.application.platform.events import (
    create_canonical_event,
    record_timeline_event,
)
from attention_router.application.platform.registry import (
    bind_provider,
    capability_and_version,
    register_provider_instance,
    resolve_provider,
    sync_platform_registry,
)
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.entities import EntityReference
from attention_router.core.events import EventOrigin
from attention_router.core.providers import ProviderResult, ProviderRuntimeRegistry
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import (
    CapabilityDefinitionRow,
    CapabilityGrantRow,
    CapabilityVersionRow,
    CommitmentRow,
    ProviderBindingRow,
    ProviderInstanceRow,
    ReminderRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.observability.tracing import safe_set_attribute, set_outcome, start_span


INTERNAL_PROVIDERS = {
    "internal:presence": "internal_presence",
    "internal:capability-registry": "internal_capability_registry",
    "internal:commitments": "internal_commitments",
    "internal:scheduler": "internal_scheduler",
    "internal:grants": "internal_grants",
    "internal:timeline": "internal_timeline",
    "internal:reply": "internal_reply",
}

CAPABILITY_PROVIDER = {
    "presence.set": "internal:presence",
    "capability.list": "internal:capability-registry",
    "capability.inspect": "internal:capability-registry",
    "commitment.create": "internal:commitments",
    "commitment.list": "internal:commitments",
    "commitment.complete": "internal:commitments",
    "commitment.cancel": "internal:commitments",
    "reminder.create": "internal:scheduler",
    "reminder.list": "internal:scheduler",
    "reminder.cancel": "internal:scheduler",
    "grant.list": "internal:grants",
    "grant.create": "internal:grants",
    "grant.revoke": "internal:grants",
    "timeline.query": "internal:timeline",
    "conversation.reply": "internal:reply",
}


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _execution(parameters: Mapping[str, Any]) -> dict[str, Any]:
    value = parameters.get("_execution") or {}
    return value if isinstance(value, dict) else {}


def provision_internal_providers(session: Session, tenant_id: str) -> dict[str, int]:
    """Materialize safe internal instances and bindings idempotently."""
    sync_platform_registry(session, tenant_id=tenant_id)
    # SessionLocal disables autoflush; instances must see the registry rows
    # materialized by the sync in this same transaction.
    session.flush()
    instances: dict[str, ProviderInstanceRow] = {}
    created = bindings = 0
    for name, definition_name in INTERNAL_PROVIDERS.items():
        row = session.scalar(
            select(ProviderInstanceRow).where(
                ProviderInstanceRow.tenant_id == tenant_id,
                ProviderInstanceRow.canonical_name == name,
            )
        )
        if row is None:
            row = register_provider_instance(
                session,
                tenant_id=tenant_id,
                provider_definition_name=definition_name,
                canonical_name=name,
                state="CONFIGURED",
                health="HEALTHY",
                config_reference={"kind": "internal"},
            )
            created += 1
        else:
            row.state, row.health, row.updated_at = "CONFIGURED", "HEALTHY", now_utc()
        instances[name] = row
    for capability_name, provider_name in CAPABILITY_PROVIDER.items():
        capability, _ = capability_and_version(session, tenant_id, capability_name)
        if capability is None:
            continue
        existing = session.scalar(
            select(ProviderBindingRow.id).where(
                ProviderBindingRow.tenant_id == tenant_id,
                ProviderBindingRow.capability_id == capability.id,
                ProviderBindingRow.status == "ACTIVE",
            )
        )
        if existing is None:
            bind_provider(
                session,
                tenant_id=tenant_id,
                capability_name=capability_name,
                provider_instance_id=instances[provider_name].id,
            )
            bindings += 1
    return {"instances_created": created, "bindings_created": bindings}


class _InternalProvider:
    def __init__(self, session: Session, tenant_id: str, interface_name: str) -> None:
        self.session, self.tenant_id, self.interface_name = session, tenant_id, interface_name

    def health(self) -> str:
        return "HEALTHY"

    def execute(self, capability: str, parameters: Mapping[str, Any]) -> ProviderResult:
        handler = getattr(self, f"_handle_{capability.replace('.', '_')}", None)
        if handler is None:
            return ProviderResult(False, reason_code="CAPABILITY_HANDLER_MISSING")
        try:
            return handler(parameters)
        except (KeyError, ValueError) as exc:
            return ProviderResult(False, reason_code=str(exc)[:120])

    def _event(self, event_type: str, params: Mapping[str, Any]) -> Any:
        execution = _execution(params)
        return create_canonical_event(
            self.session,
            tenant_id=self.tenant_id,
            requested_origin=EventOrigin.INTERNAL_EVENT,
            event_type=event_type,
            payload_type="CAPABILITY_PACK_REFERENCE",
            correlation_id=str(execution.get("correlation_id") or new_id()),
            causation_id=execution.get("causation_id"),
            actor_id=execution.get("actor_id"),
            payload_ref={"capability": execution.get("capability")},
            metadata_sanitized={"provider": execution.get("provider")},
        )


class InternalPresenceProvider(_InternalProvider):
    def _lifetime_contract(self) -> tuple[int, int]:
        _, version = capability_and_version(self.session, self.tenant_id, "presence.set")
        metadata = version.metadata_json if version else {}
        default_ttl = metadata.get("default_ttl_seconds")
        max_ttl = metadata.get("max_ttl_seconds")
        if (
            version is None
            or version.version != 3
            or metadata.get("presence_is_perishable") is not True
            or not isinstance(default_ttl, int)
            or isinstance(default_ttl, bool)
            or not isinstance(max_ttl, int)
            or isinstance(max_ttl, bool)
            or not 1 <= default_ttl <= max_ttl
        ):
            raise ValueError("PRESENCE_LIFETIME_CONTRACT_INVALID")
        return default_ttl, max_ttl

    @staticmethod
    def _resolve_expiry(
        params: Mapping[str, Any],
        *,
        execution_now: datetime,
        default_ttl: int,
        max_ttl: int,
    ) -> tuple[datetime, str, int | None]:
        has_expires_at = "expires_at" in params
        has_ttl = "ttl_seconds" in params
        if has_expires_at and has_ttl:
            raise ValueError("PRESENCE_EXPIRY_AMBIGUOUS")
        if has_expires_at:
            try:
                expires_at = _dt(params.get("expires_at"))
            except (TypeError, ValueError):
                raise ValueError("PRESENCE_EXPIRY_INVALID") from None
            if expires_at is None:
                raise ValueError("PRESENCE_EXPIRY_INVALID")
            if expires_at.tzinfo is None:
                raise ValueError("PRESENCE_EXPIRY_TIMEZONE_REQUIRED")
            expires_at = expires_at.astimezone(timezone.utc)
            if expires_at <= execution_now:
                raise ValueError("PRESENCE_EXPIRY_NOT_FUTURE")
            if expires_at > execution_now + timedelta(seconds=max_ttl):
                raise ValueError("PRESENCE_EXPIRY_EXCEEDS_MAX")
            return expires_at, "EXPLICIT_EXPIRES_AT", None
        if has_ttl:
            ttl = params.get("ttl_seconds")
            if not isinstance(ttl, int) or isinstance(ttl, bool):
                raise ValueError("PRESENCE_TTL_INVALID")
            if not 1 <= ttl <= max_ttl:
                raise ValueError("PRESENCE_TTL_OUT_OF_RANGE")
            return execution_now + timedelta(seconds=ttl), "EXPLICIT_TTL", ttl
        return (
            execution_now + timedelta(seconds=default_ttl),
            "DEFAULT_TTL",
            default_ttl,
        )

    def _handle_presence_set(self, params: Mapping[str, Any]) -> ProviderResult:
        execution_now = now_utc().astimezone(timezone.utc)
        state = str(params.get("state", "")).lower()
        if state not in {"available", "busy", "sleeping", "away", "do_not_disturb", "custom"}:
            return ProviderResult(False, reason_code="INVALID_PRESENCE_STATE")
        execution = _execution(params)
        actor_id = str(execution.get("actor_id") or "")
        if not actor_id:
            return ProviderResult(False, reason_code="ACTOR_REQUIRED")
        default_ttl, max_ttl = self._lifetime_contract()
        expires_at, expiry_source, applied_ttl = self._resolve_expiry(
            params,
            execution_now=execution_now,
            default_ttl=default_ttl,
            max_ttl=max_ttl,
        )
        event = self._event("PRESENCE_SET", params)
        state_row = set_entity_state(
            self.session,
            tenant_id=self.tenant_id,
            subject=EntityReference(entity_type="ACTOR", entity_id=actor_id),
            namespace="presence",
            key="effective",
            value={"status": state, "audience_scope": str(params.get("audience_scope", "all"))},
            source=(
                "explicit_owner_action"
                if execution.get("owner_authenticated")
                else "internal:presence"
            ),
            effective_at=execution_now,
            expires_at=expires_at,
        )
        record_timeline_event(
            self.session,
            tenant_id=self.tenant_id,
            canonical_event_id=event.id,
            actor_id=actor_id,
            event_type="STATE_CHANGED",
            occurred_at=event.occurred_at,
            provenance="internal:presence",
            event_ref={
                "state": state,
                "expires_at": expires_at.isoformat(),
                "expiry_source": expiry_source,
            },
        )
        audit(
            self.session,
            None,
            "capability_state_changed",
            {
                "capability": "presence.set",
                "state": state,
                "expiry_present": True,
                "expiry_source": expiry_source,
                "ttl_seconds": applied_ttl,
            },
            tenant_id=self.tenant_id,
        )
        return ProviderResult(
            True,
            {
                "status": state,
                "version": state_row.version,
                "expires_at": expires_at.isoformat(),
                "expiry_source": expiry_source,
            },
            "PRESENCE_SET",
        )


class InternalCapabilityRegistryProvider(_InternalProvider):
    def _safe_item(self, row: CapabilityDefinitionRow) -> dict[str, Any]:
        version = (
            self.session.get(CapabilityVersionRow, row.current_version_id)
            if row.current_version_id
            else None
        )
        provider = (
            resolve_provider(
                self.session, tenant_id=self.tenant_id, capability=row, version=version
            )
            if version
            else None
        )
        return {
            "capability_name": row.canonical_name,
            "domain": row.domain,
            "availability_state": row.availability_state,
            "operation_type": version.operation_type if version else None,
            "side_effect": version.side_effect if version else None,
            "description": row.description,
            "required_provider_interface": version.required_provider_interface if version else None,
            "bound_provider_status": provider.reason_code if provider else "VERSION_MISSING",
        }

    def _handle_capability_list(self, params: Mapping[str, Any]) -> ProviderResult:
        rows = self.session.scalars(
            select(CapabilityDefinitionRow)
            .where(CapabilityDefinitionRow.tenant_id == self.tenant_id)
            .order_by(CapabilityDefinitionRow.canonical_name)
        ).all()
        return ProviderResult(
            True, {"capabilities": [self._safe_item(row) for row in rows]}, "CAPABILITY_LISTED"
        )

    def _handle_capability_inspect(self, params: Mapping[str, Any]) -> ProviderResult:
        name = str(params.get("capability_name", ""))
        row = self.session.scalar(
            select(CapabilityDefinitionRow).where(
                CapabilityDefinitionRow.tenant_id == self.tenant_id,
                CapabilityDefinitionRow.canonical_name == name,
            )
        )
        if row is None:
            return ProviderResult(False, reason_code="UNKNOWN_CAPABILITY")
        return ProviderResult(True, self._safe_item(row), "CAPABILITY_INSPECTED")


class InternalCommitmentProvider(_InternalProvider):
    def _one(self, params: Mapping[str, Any]) -> CommitmentRow:
        row = self.session.get(CommitmentRow, str(params.get("commitment_id", "")))
        if row is None or row.tenant_id != self.tenant_id:
            raise KeyError("COMMITMENT_NOT_FOUND")
        return row

    def _handle_commitment_create(self, params: Mapping[str, Any]) -> ProviderResult:
        execution = _execution(params)
        summary = str(params.get("summary", "")).strip()
        actor_id = str(execution.get("actor_id") or "")
        if not summary or not actor_id:
            return ProviderResult(False, reason_code="COMMITMENT_SUMMARY_AND_ACTOR_REQUIRED")
        event = self._event("COMMITMENT_CREATED", params)
        row = CommitmentRow(
            id=new_id(),
            tenant_id=self.tenant_id,
            created_by_actor_id=actor_id,
            responsible_actor_id=str(params.get("responsible_actor_id") or actor_id),
            beneficiary_actor_id=params.get("beneficiary_actor_id"),
            resource_id=params.get("resource_id"),
            summary=summary,
            details=params.get("details"),
            status="OPEN",
            due_at=_dt(params.get("due_at")),
            source_event_id=event.id,
            metadata_json={},
            created_at=now_utc(),
            completed_at=None,
            cancelled_at=None,
        )
        self.session.add(row)
        self.session.flush()
        record_timeline_event(
            self.session,
            tenant_id=self.tenant_id,
            canonical_event_id=event.id,
            actor_id=row.responsible_actor_id,
            event_type="COMMITMENT_CREATED",
            occurred_at=event.occurred_at,
            provenance="internal:commitments",
            event_ref={"commitment_id": row.id},
        )
        audit(
            self.session,
            None,
            "commitment_created",
            {"commitment_id": row.id},
            tenant_id=self.tenant_id,
        )
        return ProviderResult(
            True, {"commitment_id": row.id, "status": row.status}, "COMMITMENT_CREATED"
        )

    def _handle_commitment_list(self, params: Mapping[str, Any]) -> ProviderResult:
        query = select(CommitmentRow).where(CommitmentRow.tenant_id == self.tenant_id)
        for key in ("responsible_actor_id", "beneficiary_actor_id", "status"):
            if params.get(key):
                query = query.where(getattr(CommitmentRow, key) == params[key])
        rows = self.session.scalars(
            query.order_by(CommitmentRow.created_at.desc()).limit(int(params.get("limit", 20)))
        ).all()
        return ProviderResult(
            True,
            {
                "commitments": [
                    {
                        "commitment_id": r.id,
                        "summary": r.summary,
                        "status": r.status,
                        "due_at": r.due_at.isoformat() if r.due_at else None,
                    }
                    for r in rows
                ]
            },
            "COMMITMENT_LISTED",
        )

    def _transition(self, params: Mapping[str, Any], status: str) -> ProviderResult:
        row = self._one(params)
        if row.status != "OPEN":
            return ProviderResult(False, reason_code="COMMITMENT_NOT_OPEN")
        event = self._event(f"COMMITMENT_{status}", params)
        row.status = status
        row.completed_at = now_utc() if status == "COMPLETED" else None
        row.cancelled_at = now_utc() if status == "CANCELLED" else None
        record_timeline_event(
            self.session,
            tenant_id=self.tenant_id,
            canonical_event_id=event.id,
            actor_id=row.responsible_actor_id,
            event_type=f"COMMITMENT_{status}",
            occurred_at=event.occurred_at,
            provenance="internal:commitments",
            event_ref={"commitment_id": row.id},
        )
        audit(
            self.session,
            None,
            f"commitment_{status.lower()}",
            {"commitment_id": row.id},
            tenant_id=self.tenant_id,
        )
        return ProviderResult(
            True, {"commitment_id": row.id, "status": row.status}, f"COMMITMENT_{status}"
        )

    def _handle_commitment_complete(self, params: Mapping[str, Any]) -> ProviderResult:
        return self._transition(params, "COMPLETED")

    def _handle_commitment_cancel(self, params: Mapping[str, Any]) -> ProviderResult:
        return self._transition(params, "CANCELLED")


class InternalSchedulerProvider(_InternalProvider):
    def _one(self, params: Mapping[str, Any]) -> ReminderRow:
        row = self.session.get(ReminderRow, str(params.get("reminder_id", "")))
        if row is None or row.tenant_id != self.tenant_id:
            raise KeyError("REMINDER_NOT_FOUND")
        return row

    def _handle_reminder_create(self, params: Mapping[str, Any]) -> ProviderResult:
        execution = _execution(params)
        actor_id = str(execution.get("actor_id") or "")
        trigger_at = _dt(params.get("trigger_at"))
        summary = str(params.get("summary", "")).strip()
        if not actor_id or not trigger_at or not summary:
            return ProviderResult(False, reason_code="REMINDER_FIELDS_REQUIRED")
        key = str(execution.get("idempotency_key") or execution.get("correlation_id") or new_id())
        existing = self.session.scalar(
            select(ReminderRow).where(
                ReminderRow.tenant_id == self.tenant_id, ReminderRow.idempotency_key == key
            )
        )
        if existing:
            return ProviderResult(
                True, {"reminder_id": existing.id, "status": existing.status}, "IDEMPOTENT_REPLAY"
            )
        event = self._event("REMINDER_CREATED", params)
        row = ReminderRow(
            id=new_id(),
            tenant_id=self.tenant_id,
            owner_actor_id=actor_id,
            resource_id=params.get("resource_id"),
            summary=summary,
            trigger_at=trigger_at,
            status="SCHEDULED",
            source_event_id=event.id,
            correlation_id=str(execution.get("correlation_id") or event.correlation_id),
            idempotency_key=key,
            created_at=now_utc(),
            fired_at=None,
            cancelled_at=None,
            claimed_at=None,
            claim_token=None,
        )
        self.session.add(row)
        self.session.flush()
        record_timeline_event(
            self.session,
            tenant_id=self.tenant_id,
            canonical_event_id=event.id,
            actor_id=actor_id,
            event_type="REMINDER_SCHEDULED",
            occurred_at=event.occurred_at,
            provenance="internal:scheduler",
            event_ref={"reminder_id": row.id},
        )
        audit(
            self.session,
            None,
            "reminder_scheduled",
            {"reminder_id": row.id},
            tenant_id=self.tenant_id,
        )
        return ProviderResult(
            True, {"reminder_id": row.id, "status": row.status}, "REMINDER_SCHEDULED"
        )

    def _handle_reminder_list(self, params: Mapping[str, Any]) -> ProviderResult:
        query = select(ReminderRow).where(ReminderRow.tenant_id == self.tenant_id)
        if params.get("owner_actor_id"):
            query = query.where(ReminderRow.owner_actor_id == params["owner_actor_id"])
        if params.get("status"):
            query = query.where(ReminderRow.status == params["status"])
        rows = self.session.scalars(
            query.order_by(ReminderRow.trigger_at).limit(int(params.get("limit", 20)))
        ).all()
        return ProviderResult(
            True,
            {
                "reminders": [
                    {
                        "reminder_id": r.id,
                        "summary": r.summary,
                        "status": r.status,
                        "trigger_at": r.trigger_at.isoformat(),
                    }
                    for r in rows
                ]
            },
            "REMINDER_LISTED",
        )

    def _handle_reminder_cancel(self, params: Mapping[str, Any]) -> ProviderResult:
        row = self._one(params)
        if row.status != "SCHEDULED":
            return ProviderResult(False, reason_code="REMINDER_NOT_SCHEDULED")
        event = self._event("REMINDER_CANCELLED", params)
        row.status, row.cancelled_at = "CANCELLED", now_utc()
        record_timeline_event(
            self.session,
            tenant_id=self.tenant_id,
            canonical_event_id=event.id,
            actor_id=row.owner_actor_id,
            event_type="REMINDER_CANCELLED",
            occurred_at=event.occurred_at,
            provenance="internal:scheduler",
            event_ref={"reminder_id": row.id},
        )
        return ProviderResult(
            True, {"reminder_id": row.id, "status": row.status}, "REMINDER_CANCELLED"
        )


class InternalGrantProvider(_InternalProvider):
    def _owner(self, params: Mapping[str, Any]) -> str:
        execution = _execution(params)
        actor = str(execution.get("actor_id") or "")
        if not execution.get("owner_authenticated") or not actor:
            raise KeyError("OWNER_AUTHORITY_REQUIRED")
        return actor

    def _handle_grant_list(self, params: Mapping[str, Any]) -> ProviderResult:
        query = (
            select(CapabilityGrantRow, CapabilityDefinitionRow)
            .join(
                CapabilityDefinitionRow,
                CapabilityDefinitionRow.id == CapabilityGrantRow.capability_id,
            )
            .where(CapabilityGrantRow.tenant_id == self.tenant_id)
        )
        if params.get("grantee_id"):
            query = query.where(CapabilityGrantRow.grantee_id == params["grantee_id"])
        rows = self.session.execute(
            query.order_by(CapabilityGrantRow.created_at.desc()).limit(int(params.get("limit", 20)))
        ).all()
        now = now_utc()
        return ProviderResult(
            True,
            {
                "grants": [
                    {
                        "grant_id": g.id,
                        "capability": c.canonical_name,
                        "grantee_id": g.grantee_id,
                        "status": "EXPIRED"
                        if g.status == "ACTIVE" and g.valid_until and g.valid_until <= now
                        else g.status,
                        "valid_from": g.valid_from.isoformat(),
                        "valid_until": g.valid_until.isoformat() if g.valid_until else None,
                    }
                    for g, c in rows
                ]
            },
            "GRANT_LISTED",
        )

    def _handle_grant_create(self, params: Mapping[str, Any]) -> ProviderResult:
        owner = self._owner(params)
        grantee = str(params.get("grantee_id") or "")
        capability = str(params.get("capability_name") or "")
        if not grantee or grantee == owner:
            return ProviderResult(False, reason_code="GRANTEE_SELF_ESCALATION_DENIED")
        valid_from, valid_until = _dt(params.get("valid_from")), _dt(params.get("valid_until"))
        if valid_until and valid_from and valid_until <= valid_from:
            return ProviderResult(False, reason_code="INVALID_GRANT_WINDOW")
        event = self._event("GRANT_CREATED", params)
        row = create_capability_grant(
            self.session,
            tenant_id=self.tenant_id,
            grantor_type="ACTOR",
            grantor_id=owner,
            grantee_type="ACTOR",
            grantee_id=grantee,
            capability_name=capability,
            target_resource_id=params.get("target_resource_id"),
            valid_from=valid_from,
            valid_until=valid_until,
            provenance="internal:grants",
        )
        return ProviderResult(
            True, {"grant_id": row.id, "status": row.status, "event_id": event.id}, "GRANT_CREATED"
        )

    def _handle_grant_revoke(self, params: Mapping[str, Any]) -> ProviderResult:
        owner = self._owner(params)
        row = self.session.get(CapabilityGrantRow, str(params.get("grant_id", "")))
        if row is None or row.tenant_id != self.tenant_id:
            return ProviderResult(False, reason_code="GRANT_NOT_FOUND")
        if row.grantor_id != owner:
            return ProviderResult(False, reason_code="GRANT_REVOKE_NOT_OWNER")
        revoke_capability_grant(self.session, tenant_id=self.tenant_id, grant_id=row.id)
        row.revoked_by, row.revocation_reason = owner, str(params.get("reason") or "")[:240] or None
        return ProviderResult(True, {"grant_id": row.id, "status": row.status}, "GRANT_REVOKED")


class InternalReplyProvider:
    interface_name = "InternalReplyProvider"

    def __init__(self, session: Session, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def health(self) -> str:
        return "HEALTHY"

    def execute(self, capability: str, parameters: Mapping[str, Any]) -> ProviderResult:
        if capability != "conversation.reply":
            return ProviderResult(False, reason_code="CAPABILITY_NOT_SUPPORTED")
        intent_id = (parameters.get("_execution") or {}).get("execution_intent_id")
        if not intent_id:
            return ProviderResult(False, reason_code="EXECUTION_INTENT_REQUIRED")
        # Release/authorization and the safety reservation remain owned by the
        # existing execution service; this adapter only enters its outbox stage.
        from attention_router.application.execution import enqueue_ready_intents
        count = enqueue_ready_intents(
            self.session, limit=1, transport_ready=True, execution_intent_id=intent_id
        )
        if count != 1:
            return ProviderResult(False, reason_code="CONTROLLED_EXECUTION_NOT_QUEUED")
        return ProviderResult(True, {"delegated_to": "controlled_execution_outbox", "execution_intent_id": intent_id}, "DELEGATED")


class InternalTimelineProvider(_InternalProvider):
    def _handle_timeline_query(self, params: Mapping[str, Any]) -> ProviderResult:
        query = select(TimelineEventRow).where(TimelineEventRow.tenant_id == self.tenant_id)
        for key in ("actor_id", "relationship_id", "resource_id", "event_type"):
            if params.get(key):
                query = query.where(getattr(TimelineEventRow, key) == params[key])
        if params.get("from_at"):
            query = query.where(TimelineEventRow.occurred_at >= _dt(params["from_at"]))
        if params.get("until_at"):
            query = query.where(TimelineEventRow.occurred_at <= _dt(params["until_at"]))
        rows = self.session.scalars(
            query.order_by(TimelineEventRow.occurred_at.desc()).limit(
                min(int(params.get("limit", 20)), 100)
            )
        ).all()
        return ProviderResult(
            True,
            {
                "coverage": "recorded_timeline_only",
                "events": [
                    {
                        "event_type": r.event_type,
                        "occurred_at": r.occurred_at.isoformat(),
                        "provenance": r.provenance,
                        "event_ref": r.event_ref,
                    }
                    for r in rows
                ],
            },
            "TIMELINE_QUERIED",
        )


def internal_runtime_registry(session: Session, tenant_id: str) -> ProviderRuntimeRegistry:
    provision_internal_providers(session, tenant_id)
    registry = ProviderRuntimeRegistry()
    implementations = {
        "internal:presence": InternalPresenceProvider(
            session, tenant_id, "InternalPresenceProvider"
        ),
        "internal:capability-registry": InternalCapabilityRegistryProvider(
            session, tenant_id, "InternalCapabilityRegistryProvider"
        ),
        "internal:commitments": InternalCommitmentProvider(
            session, tenant_id, "InternalCommitmentProvider"
        ),
        "internal:scheduler": InternalSchedulerProvider(
            session, tenant_id, "InternalSchedulerProvider"
        ),
        "internal:grants": InternalGrantProvider(session, tenant_id, "InternalGrantProvider"),
        "internal:timeline": InternalTimelineProvider(
            session, tenant_id, "InternalTimelineProvider"
        ),
        "internal:reply": InternalReplyProvider(session, tenant_id),
    }
    for name, implementation in implementations.items():
        instance = session.scalar(
            select(ProviderInstanceRow).where(
                ProviderInstanceRow.tenant_id == tenant_id,
                ProviderInstanceRow.canonical_name == name,
            )
        )
        if instance:
            registry.register(instance.id, implementation)
    return registry


def execute_owner_capability(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    request: CapabilityRequest,
    correlation_id: str,
    causation_id: str | None = None,
) -> Any:
    """One authenticated owner entrypoint, retaining generic authority/provider resolution."""
    from attention_router.application.platform.execution import execute_capability

    runtime = internal_runtime_registry(session, tenant_id)
    params = dict(request.parameters)
    params["_execution"] = {
        "actor_id": actor_id,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "idempotency_key": correlation_id,
        "capability": request.capability,
        "owner_authenticated": True,
    }
    enriched = request.model_copy(update={"parameters": params})
    return execute_capability(
        session,
        enriched,
        tenant_id=tenant_id,
        grantee_type="ACTOR",
        grantee_id=actor_id,
        policy_allows=True,
        runtime_registry=runtime,
        owner_authorized=True,
    )


def process_due_scheduled_events(
    session: Session, *, now: datetime | None = None, limit: int = 20
) -> int:
    """Claim and fire due reminders atomically; firing creates no external delivery."""
    now = now or now_utc()
    fired = 0
    candidates = session.scalars(
        select(ReminderRow.id)
        .where(ReminderRow.status == "SCHEDULED", ReminderRow.trigger_at <= now)
        .order_by(ReminderRow.trigger_at)
        .limit(limit)
    ).all()
    for reminder_id in candidates:
        token = new_id()
        claimed = session.execute(
            update(ReminderRow)
            .where(
                ReminderRow.id == reminder_id,
                ReminderRow.status == "SCHEDULED",
                ReminderRow.claim_token.is_(None),
            )
            .values(claim_token=token, claimed_at=now)
        )
        if claimed.rowcount != 1:
            continue
        row = session.get(ReminderRow, reminder_id)
        with start_span("scheduler.fire") as span:
            safe_set_attribute(span, "attention.tenant_id", row.tenant_id)
            event = create_canonical_event(
                session,
                tenant_id=row.tenant_id,
                requested_origin=EventOrigin.SCHEDULED_EVENT,
                event_type="REMINDER_FIRED",
                payload_type="REMINDER_REFERENCE",
                correlation_id=row.correlation_id,
                causation_id=row.source_event_id,
                actor_id=row.owner_actor_id,
                payload_ref={"reminder_id": row.id},
                occurred_at=now,
                metadata_sanitized={"delivery": "blocked"},
            )
            row.status, row.fired_at, row.claim_token = "FIRED", now, None
            record_timeline_event(
                session,
                tenant_id=row.tenant_id,
                canonical_event_id=event.id,
                actor_id=row.owner_actor_id,
                event_type="REMINDER_FIRED",
                occurred_at=now,
                provenance="internal:scheduler",
                event_ref={"reminder_id": row.id},
            )
            audit(
                session,
                None,
                "reminder_fired",
                {"reminder_id": row.id, "external_delivery": False},
                tenant_id=row.tenant_id,
            )
            set_outcome(span, "FIRED")
            fired += 1
    return fired
