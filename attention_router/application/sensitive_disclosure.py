"""Durable owner-approved disclosure of sensitive data to conversation peers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.assistant_provenance import label_automated_text
from attention_router.application.platform.capability_pack import execute_owner_capability
from attention_router.application.platform.context import RepresentedSubjectIdentity
from attention_router.config import settings
from attention_router.core.capabilities import CapabilityRequest
from attention_router.infrastructure.models import (
    ActorBindingRow,
    ExecutionIntentRow,
    HumanExecutionAuthorizationRow,
    InteractionRow,
    OutboxMessageRow,
    SensitiveDisclosureRequestRow,
)
from attention_router.infrastructure.repository import audit
from attention_router.platform.human_execution_authorization import (
    fingerprint,
    prepare,
    request_client_approval,
)


DISCLOSURE_OUTBOX_ACTION = "sensitive_disclosure_text"
LOCATION_CAPABILITY = "location.current"


@dataclass(frozen=True, slots=True)
class DisclosurePreparation:
    status: str
    response_text: str
    request_id: str | None = None
    missing_information: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:limit]


def _owner_label(identity: RepresentedSubjectIdentity) -> str:
    return identity.reference_name or "o titular da conta"


def _requester_identity(
    interaction: InteractionRow,
    binding: ActorBindingRow | None,
    parameters: dict,
) -> tuple[str | None, str | None, str]:
    bound_name = _clean(binding.display_name if binding is not None else None, 160)
    metadata = binding.binding_metadata if binding is not None else {}
    bound_relationship = _clean((metadata or {}).get("relationship"), 120)
    if bound_relationship is None:
        candidate = _clean(interaction.relationship_category, 120)
        if candidate and candidate.casefold() not in {
            "unknown",
            "desconhecido",
            "none",
            "n/a",
        }:
            bound_relationship = candidate

    reported_name = _clean(parameters.get("requester_name"), 160)
    reported_relationship = _clean(parameters.get("requester_relationship"), 120)
    name = bound_name or reported_name
    relationship = bound_relationship or reported_relationship
    if bound_name and bound_relationship:
        source = "BOUND"
    elif (reported_name or reported_relationship) and (bound_name or bound_relationship):
        source = "MIXED"
    else:
        source = "SELF_REPORTED"
    return name, relationship, source


def _identity_question(*, owner_label: str, missing: tuple[str, ...]) -> str:
    if missing == ("requester_name", "requester_relationship"):
        return (
            f"Não tenho autorização para compartilhar a localização de {owner_label}. "
            "Posso solicitar autorização, mas antes preciso confirmar seu nome e "
            "seu grau de parentesco ou relação com essa pessoa."
        )
    if "requester_name" in missing:
        return (
            f"Não tenho autorização para compartilhar a localização de {owner_label}. "
            "Antes de solicitar autorização, preciso confirmar seu nome."
        )
    return (
        f"Não tenho autorização para compartilhar a localização de {owner_label}. "
        "Antes de solicitar autorização, preciso confirmar seu grau de parentesco "
        "ou relação com essa pessoa."
    )


def prepare_location_disclosure(
    session: Session,
    *,
    interaction: InteractionRow,
    source_event_id: str,
    binding: ActorBindingRow | None,
    represented_identity: RepresentedSubjectIdentity | None,
    recipient_reference: str,
    parameters: dict,
    ttl_seconds: int = 600,
    now: datetime | None = None,
) -> DisclosurePreparation:
    """Create one Android approval request; never read location here."""
    timestamp = now or _now()
    if represented_identity is None:
        return DisclosurePreparation(
            status="OWNER_IDENTITY_UNAVAILABLE",
            response_text=(
                "Não tenho autorização para compartilhar essa informação e não "
                "consigo identificar com segurança o titular da conta."
            ),
        )
    owner_label = _owner_label(represented_identity)
    name, relationship, identity_source = _requester_identity(
        interaction,
        binding,
        parameters,
    )
    missing = tuple(
        item
        for item, value in (
            ("requester_name", name),
            ("requester_relationship", relationship),
        )
        if not value
    )
    if missing:
        return DisclosurePreparation(
            status="NEEDS_REQUESTER_IDENTITY",
            response_text=_identity_question(owner_label=owner_label, missing=missing),
            missing_information=missing,
        )
    if not settings.client_approval_enabled or not settings.client_session_enabled:
        return DisclosurePreparation(
            status="APPROVAL_CHANNEL_UNAVAILABLE",
            response_text=(
                f"Não tenho autorização para compartilhar a localização de {owner_label} "
                "e o canal de autorização está indisponível no momento."
            ),
        )

    existing = session.scalar(
        select(SensitiveDisclosureRequestRow).where(
            SensitiveDisclosureRequestRow.source_interaction_id == interaction.id,
            SensitiveDisclosureRequestRow.capability == LOCATION_CAPABILITY,
        )
    )
    if existing is not None:
        return DisclosurePreparation(
            status=existing.state,
            request_id=existing.id,
            response_text=(
                f"A solicitação de autorização para a localização de {owner_label} "
                "já está registrada. Assim que houver uma decisão válida, eu retorno "
                "por esta conversa."
            ),
        )

    preview = (
        f"{name} solicitou acesso à sua localização atual. "
        f"Relação informada: {relationship}. Aprovar permite uma consulta única "
        "à localização atual e o retorno da informação nesta conversa."
    )
    frozen_scope = {
        "frozen_authority": {
            "capability": LOCATION_CAPABILITY,
            "operation": "sensitive_disclosure.location.current",
        },
        "target": {
            "tenant": interaction.tenant_id,
            "canonical_address": name,
        },
        "immutable_inputs": {
            "effective_response_snapshot": preview,
        },
        "sensitive_disclosure": {
            "source_interaction_id": interaction.id,
            "source_event_id": source_event_id,
            "requester_contact_id": interaction.contact_id,
            "requester_display_name": name,
            "requester_relationship": relationship,
            "recipient_reference": recipient_reference,
            "represented_owner_actor_key": represented_identity.actor.entity_id,
            "represented_owner_human_identity_id": represented_identity.human_identity_id,
        },
    }
    scope_fingerprint = fingerprint(frozen_scope)
    expires_at = timestamp + timedelta(seconds=ttl_seconds)
    intent = ExecutionIntentRow(
        id=str(uuid4()),
        idempotency_key=f"sensitive-disclosure:{interaction.id}:{LOCATION_CAPABILITY}",
        scope=frozen_scope,
        scope_fingerprint=scope_fingerprint,
        provenance={
            "kind": "third_party_sensitive_disclosure_request",
            "source_interaction_id": interaction.id,
            "source_event_id": source_event_id,
        },
        state="FROZEN",
        created_at=timestamp,
        frozen_at=timestamp,
        expires_at=expires_at,
    )
    session.add(intent)
    session.flush()

    authorization = prepare(
        session,
        execution_intent_id=intent.id,
        expected_approver=represented_identity.human_identity_id,
        ttl_seconds=ttl_seconds,
        correlation_id=interaction.correlation_id,
        now=timestamp,
        approval_channel="android_client",
    )
    request_client_approval(session, authorization.id, now=timestamp)
    row = SensitiveDisclosureRequestRow(
        id=str(uuid4()),
        tenant_id=interaction.tenant_id,
        source_interaction_id=interaction.id,
        source_event_id=source_event_id,
        requester_actor_binding_id=binding.id if binding is not None else None,
        requester_contact_id=interaction.contact_id,
        requester_display_name=name,
        requester_relationship=relationship,
        requester_identity_source=identity_source,
        represented_owner_actor_key=represented_identity.actor.entity_id,
        represented_owner_human_identity_id=represented_identity.human_identity_id,
        represented_owner_reference_name=represented_identity.reference_name,
        capability=LOCATION_CAPABILITY,
        recipient_reference=recipient_reference,
        execution_intent_id=intent.id,
        authorization_id=authorization.id,
        state="PENDING_APPROVAL",
        result={},
        expires_at=expires_at,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(row)
    audit(
        session,
        interaction.id,
        "sensitive_disclosure.approval_requested",
        {
            "request_id": row.id,
            "authorization_id": authorization.id,
            "capability": LOCATION_CAPABILITY,
            "requester_identity_source": identity_source,
        },
        correlation_id=interaction.correlation_id,
        causation_id=source_event_id,
        origin="sensitive_disclosure",
        tenant_id=interaction.tenant_id,
    )
    session.flush()
    return DisclosurePreparation(
        status="PENDING_APPROVAL",
        request_id=row.id,
        response_text=(
            f"{name}, não tenho autorização para compartilhar a localização de "
            f"{owner_label} diretamente. Enviei uma solicitação de autorização "
            "para o aplicativo. Se houver aprovação, eu retorno por aqui."
        ),
    )


def _scope_matches(row: SensitiveDisclosureRequestRow, intent: ExecutionIntentRow) -> bool:
    scope = intent.scope if isinstance(intent.scope, dict) else {}
    frozen = scope.get("sensitive_disclosure")
    if not isinstance(frozen, dict):
        return False
    return frozen == {
        "source_interaction_id": row.source_interaction_id,
        "source_event_id": row.source_event_id,
        "requester_contact_id": row.requester_contact_id,
        "requester_display_name": row.requester_display_name,
        "requester_relationship": row.requester_relationship,
        "recipient_reference": row.recipient_reference,
        "represented_owner_actor_key": row.represented_owner_actor_key,
        "represented_owner_human_identity_id": row.represented_owner_human_identity_id,
    }


def _location_response(
    row: SensitiveDisclosureRequestRow,
    *,
    approved: bool,
    result: dict | None = None,
) -> str:
    owner_label = row.represented_owner_reference_name or "o titular da conta"
    if not approved:
        return (
            f"A solicitação de localização de {owner_label} não foi autorizada. "
            "Não vou compartilhar essa informação."
        )
    if not result:
        return (
            f"A autorização foi concedida, mas não consegui obter uma localização "
            f"atual válida de {owner_label} agora."
        )
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    accuracy = result.get("accuracy_m")
    age = result.get("age_seconds")
    freshness = result.get("freshness_state")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return (
            f"A autorização foi concedida, mas não consegui obter uma localização "
            f"atual válida de {owner_label} agora."
        )
    details = [f"latitude {latitude:.6f}", f"longitude {longitude:.6f}"]
    if isinstance(accuracy, (int, float)):
        details.append(f"precisão aproximada de {accuracy:.0f} m")
    if isinstance(age, int):
        details.append(f"capturada há {age} s")
    if freshness == "STALE":
        details.append("dado marcado como desatualizado")
    return (
        f"A solicitação foi autorizada. Localização de {owner_label}: "
        + ", ".join(details)
        + "."
    )


def _enqueue_response(
    session: Session,
    row: SensitiveDisclosureRequestRow,
    *,
    text: str,
    now: datetime,
) -> OutboxMessageRow:
    existing = (
        session.get(OutboxMessageRow, row.response_outbox_id)
        if row.response_outbox_id
        else None
    )
    if existing is not None:
        return existing
    interaction = session.get(InteractionRow, row.source_interaction_id)
    if interaction is None:
        raise RuntimeError("SENSITIVE_DISCLOSURE_INTERACTION_MISSING")
    outbox = OutboxMessageRow(
        id=str(uuid4()),
        interaction_id=row.source_interaction_id,
        action_type=DISCLOSURE_OUTBOX_ACTION,
        destination="local_transport",
        payload={
            "external_actor_id": row.recipient_reference,
            "message_type": "text",
            "text": label_automated_text(text),
            "sensitive_disclosure_request_id": row.id,
        },
        status="PENDING",
        created_at=now,
        available_at=now,
        attempt_count=0,
        idempotency_key=f"sensitive-disclosure:{row.id}:response",
        correlation_id=interaction.correlation_id,
        causation_id=row.authorization_id,
    )
    session.add(outbox)
    session.flush()
    row.response_outbox_id = outbox.id
    row.state = "RESPONSE_PENDING"
    row.updated_at = now
    audit(
        session,
        row.source_interaction_id,
        "sensitive_disclosure.response_enqueued",
        {
            "request_id": row.id,
            "outbox_id": outbox.id,
            "capability": row.capability,
        },
        correlation_id=outbox.correlation_id,
        causation_id=row.authorization_id,
        origin="sensitive_disclosure",
        tenant_id=row.tenant_id,
    )
    return outbox


def process_sensitive_disclosures(
    session: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    """Advance human decisions into exactly one location read and one reply."""
    timestamp = now or _now()
    rows = session.scalars(
        select(SensitiveDisclosureRequestRow)
        .where(SensitiveDisclosureRequestRow.state == "PENDING_APPROVAL")
        .order_by(
            SensitiveDisclosureRequestRow.created_at,
            SensitiveDisclosureRequestRow.id,
        )
        .limit(limit)
    ).all()
    processed = 0
    for row in rows:
        authorization = session.get(
            HumanExecutionAuthorizationRow,
            row.authorization_id,
        )
        intent = session.get(ExecutionIntentRow, row.execution_intent_id)
        if authorization is None or intent is None or not _scope_matches(row, intent):
            row.state = "FAILED"
            row.failure_reason = "SENSITIVE_DISCLOSURE_AUTHORITY_MISMATCH"
            row.updated_at = timestamp
            processed += 1
            continue

        if (
            authorization.state == "PENDING_HUMAN_APPROVAL"
            and _aware(timestamp) >= _aware(row.expires_at)
        ):
            authorization.state = "EXPIRED"
            authorization.updated_at = timestamp

        if authorization.state in {"EXPIRED", "REVOKED"}:
            row.result = {"decision": authorization.state}
            row.state = "EXPIRED"
            _enqueue_response(
                session,
                row,
                text=(
                    "A solicitação de localização expirou ou deixou de ser válida. "
                    "Não vou compartilhar essa informação."
                ),
                now=timestamp,
            )
            processed += 1
            continue

        if authorization.state == "DENIED":
            row.result = {"decision": "DENIED"}
            _enqueue_response(
                session,
                row,
                text=_location_response(row, approved=False),
                now=timestamp,
            )
            processed += 1
            continue

        if authorization.state != "APPROVED":
            continue

        execution = execute_owner_capability(
            session,
            tenant_id=row.tenant_id,
            actor_id=row.represented_owner_actor_key,
            request=CapabilityRequest(
                capability=LOCATION_CAPABILITY,
                parameters={},
                user_requested=True,
                confidence="high",
            ),
            correlation_id=f"{row.id}:approved",
            causation_id=row.authorization_id,
        )
        execution_result = (
            dict(execution.result)
            if isinstance(execution.result, dict)
            else {}
        )
        row.result = {
            "decision": "APPROVED",
            "execution_status": execution.status,
            "reason_code": execution.reason_code,
            "location": execution_result,
        }
        response = _location_response(
            row,
            approved=True,
            result=execution_result if execution.status == "EXECUTED" else None,
        )
        _enqueue_response(session, row, text=response, now=timestamp)
        processed += 1
    if processed:
        session.flush()
    return processed


def mark_sensitive_disclosure_delivered(
    session: Session,
    *,
    outbox_id: str,
    now: datetime | None = None,
) -> SensitiveDisclosureRequestRow | None:
    timestamp = now or _now()
    row = session.scalar(
        select(SensitiveDisclosureRequestRow).where(
            SensitiveDisclosureRequestRow.response_outbox_id == outbox_id
        )
    )
    if row is None:
        return None
    authorization = session.get(
        HumanExecutionAuthorizationRow,
        row.authorization_id,
    )
    if authorization is not None and authorization.state == "APPROVED":
        authorization.state = "CONSUMED"
        authorization.updated_at = timestamp
    row.state = "RESPONDED"
    row.completed_at = timestamp
    row.updated_at = timestamp
    audit(
        session,
        row.source_interaction_id,
        "sensitive_disclosure.response_delivered",
        {
            "request_id": row.id,
            "outbox_id": outbox_id,
            "capability": row.capability,
        },
        causation_id=row.authorization_id,
        origin="sensitive_disclosure",
        tenant_id=row.tenant_id,
    )
    session.flush()
    return row


__all__ = [
    "DISCLOSURE_OUTBOX_ACTION",
    "DisclosurePreparation",
    "LOCATION_CAPABILITY",
    "mark_sensitive_disclosure_delivered",
    "prepare_location_disclosure",
    "process_sensitive_disclosures",
]
