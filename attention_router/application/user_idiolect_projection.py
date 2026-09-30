from __future__ import annotations

from datetime import timedelta
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.user_idiolect import normalize_user_expression
from attention_router.application.owner_control_semantic_registry import (
    OwnerSemanticRegistryError,
    normalize_semantic_parameters,
)
from attention_router.application.platform.entities import record_fact
from attention_router.core.entities import EntityReference, FactClass
from attention_router.infrastructure.models import (
    ClientCommandMessageRow,
    FactRow,
    InboundEventRow,
    PendingIntentRow,
)


CONFIRMED_LANGUAGE_TTL = timedelta(days=7)
PREDICATE = "idiolect.pragmatic_mapping"
SOURCE_TYPE = "INTENT_CLARIFICATION"


class IdiolectProjectionError(RuntimeError):
    pass


def _selected_candidate(row: PendingIntentRow) -> dict:
    candidate_set = row.candidate_set or {}
    candidates = candidate_set.get("candidates")
    if not isinstance(candidates, list) or not row.selected_candidate_key:
        raise IdiolectProjectionError("IDIOLECT_CANDIDATE_SET_INVALID")
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and candidate.get("candidate_key") == row.selected_candidate_key
        ):
            return candidate
    raise IdiolectProjectionError("IDIOLECT_SELECTED_CANDIDATE_NOT_FOUND")




def _meaning_signature(value: dict[str, object]) -> tuple[str, tuple[tuple[str, object], ...]] | None:
    semantic_key = value.get("semantic_intent_key")
    parameters = value.get("parameters")
    if not isinstance(semantic_key, str) or not isinstance(parameters, dict):
        return None
    return semantic_key, tuple(sorted(parameters.items()))


def _superseded_fact_id_for_correction(
    session: Session,
    *,
    row: PendingIntentRow,
    normalized_expression: str,
    semantic_intent_key: str,
    parameters: dict[str, object],
) -> str | None:
    superseded_ids = select(FactRow.supersedes_fact_id).where(
        FactRow.tenant_id == row.tenant_id,
        FactRow.subject_type == "ACTOR",
        FactRow.subject_id == row.represented_owner_actor_key,
        FactRow.predicate == PREDICATE,
        FactRow.supersedes_fact_id.is_not(None),
    )
    candidates = session.scalars(
        select(FactRow)
        .where(
            FactRow.tenant_id == row.tenant_id,
            FactRow.subject_type == "ACTOR",
            FactRow.subject_id == row.represented_owner_actor_key,
            FactRow.predicate == PREDICATE,
            FactRow.fact_class == FactClass.USER_CONFIRMED_LANGUAGE.value,
            FactRow.id.not_in(superseded_ids),
            or_(FactRow.valid_from.is_(None), FactRow.valid_from <= row.resolved_at),
            or_(FactRow.valid_until.is_(None), FactRow.valid_until > row.resolved_at),
        )
        .order_by(FactRow.observed_at.desc())
        .limit(20)
    ).all()

    current_signature = (
        semantic_intent_key,
        tuple(sorted(parameters.items())),
    )
    matches: list[FactRow] = []
    for fact in candidates:
        value = fact.value_json or {}
        scope = value.get("context_scope")
        if not isinstance(scope, dict):
            continue
        if value.get("normalized_expression") != normalized_expression:
            continue
        if value.get("direction") != "USER_TO_ANDY_LANGUAGE":
            continue
        if scope.get("channel") != row.source_channel:
            continue
        if scope.get("conversation_key_hash") != row.conversation_key_hash:
            continue
        matches.append(fact)

    if len(matches) != 1:
        return None
    previous_signature = _meaning_signature(matches[0].value_json or {})
    if previous_signature is None or previous_signature == current_signature:
        return None
    return matches[0].id

def project_resolved_pending_intent_language_fact(
    session: Session,
    *,
    pending_intent_id: str,
) -> FactRow | None:
    row = session.get(PendingIntentRow, pending_intent_id)
    if row is None:
        raise IdiolectProjectionError("IDIOLECT_PENDING_INTENT_NOT_FOUND")
    if row.state != "RESOLVED":
        return None
    if row.resolved_at is None:
        raise IdiolectProjectionError("IDIOLECT_RESOLUTION_INCOMPLETE")
    has_inbound_resolution = row.resolution_inbound_event_id is not None
    has_client_resolution = row.resolution_client_command_id is not None
    if has_inbound_resolution == has_client_resolution:
        raise IdiolectProjectionError("IDIOLECT_RESOLUTION_INCOMPLETE")

    existing = session.scalar(
        select(FactRow).where(
            FactRow.tenant_id == row.tenant_id,
            FactRow.subject_type == "ACTOR",
            FactRow.subject_id == row.represented_owner_actor_key,
            FactRow.fact_class == FactClass.USER_CONFIRMED_LANGUAGE.value,
            FactRow.source_type == SOURCE_TYPE,
            FactRow.source_ref == row.id,
        )
    )
    if existing is not None:
        return existing

    if row.source_client_command_id is not None:
        source_command = session.get(
            ClientCommandMessageRow,
            row.source_client_command_id,
        )
        if source_command is None or source_command.tenant_id != row.source_tenant_id:
            raise IdiolectProjectionError("IDIOLECT_SOURCE_SCOPE_INVALID")
        expression = source_command.input_text
    else:
        source = session.get(InboundEventRow, row.source_inbound_event_id)
        if source is None or source.tenant_id != row.source_tenant_id:
            raise IdiolectProjectionError("IDIOLECT_SOURCE_SCOPE_INVALID")
        expression = (source.payload or {}).get("content")

    if row.resolution_client_command_id is not None:
        resolution_command = session.get(
            ClientCommandMessageRow,
            row.resolution_client_command_id,
        )
        if (
            resolution_command is None
            or resolution_command.tenant_id != row.source_tenant_id
        ):
            raise IdiolectProjectionError("IDIOLECT_SOURCE_SCOPE_INVALID")
    else:
        resolution = session.get(InboundEventRow, row.resolution_inbound_event_id)
        if resolution is None or resolution.tenant_id != row.source_tenant_id:
            raise IdiolectProjectionError("IDIOLECT_SOURCE_SCOPE_INVALID")

    if not isinstance(expression, str) or not expression.strip():
        raise IdiolectProjectionError("IDIOLECT_SOURCE_EXPRESSION_INVALID")

    candidate = _selected_candidate(row)
    semantic_key = candidate.get("semantic_intent_key")
    parameters = candidate.get("parameters")
    if not isinstance(semantic_key, str) or not isinstance(parameters, dict):
        raise IdiolectProjectionError("IDIOLECT_CANDIDATE_INVALID")
    try:
        normalized_parameters = normalize_semantic_parameters(
            semantic_key,
            parameters,
        )
    except OwnerSemanticRegistryError as exc:
        raise IdiolectProjectionError(str(exc)) from exc

    normalized_expression = normalize_user_expression(expression)
    value = {
        "expression": expression.strip(),
        "normalized_expression": normalized_expression,
        "meaning_kind": "SEMANTIC_INTENT",
        "semantic_intent_key": semantic_key,
        "parameters": normalized_parameters,
        "context_scope": {
            "channel": row.source_channel,
            "conversation_key_hash": row.conversation_key_hash,
        },
        "direction": "USER_TO_ANDY_LANGUAGE",
        "evidence_class": "EXPLICITLY_CONFIRMED",
        "reuse_policy": "INTERPRET_ONLY",
        "generalization_scope": "CONVERSATION",
        "evidence_confidence": 1.0,
        "generalization_confidence": 0.25,
    }
    return record_fact(
        session,
        tenant_id=row.tenant_id,
        subject=EntityReference(
            entity_type="ACTOR",
            entity_id=row.represented_owner_actor_key,
        ),
        predicate=PREDICATE,
        fact_class=FactClass.USER_CONFIRMED_LANGUAGE,
        source_type=SOURCE_TYPE,
        confidence=1.0,
        value=value,
        source_ref=row.id,
        valid_from=row.resolved_at,
        valid_until=row.resolved_at + CONFIRMED_LANGUAGE_TTL,
        supersedes_fact_id=_superseded_fact_id_for_correction(
            session,
            row=row,
            normalized_expression=normalized_expression,
            semantic_intent_key=semantic_key,
            parameters=normalized_parameters,
        ),
        metadata_sanitized={
            "pending_intent_id": row.id,
            "source_tenant_id": row.source_tenant_id,
            "source_inbound_event_id": row.source_inbound_event_id,
            "source_client_command_id": row.source_client_command_id,
            "resolution_inbound_event_id": row.resolution_inbound_event_id,
            "resolution_client_command_id": row.resolution_client_command_id,
            "candidate_set_fingerprint": row.candidate_set_fingerprint,
            "sensitivity": "PRIVATE",
        },
    )


__all__ = [
    "CONFIRMED_LANGUAGE_TTL",
    "IdiolectProjectionError",
    "PREDICATE",
    "SOURCE_TYPE",
    "project_resolved_pending_intent_language_fact",
]
