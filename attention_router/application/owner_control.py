from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.owner_automation_control import (
    OwnerAutomationControlMutation,
    set_automatic_responses_enabled,
)
from attention_router.application.owner_operational_control import (
    OwnerReplyGraceCommandType,
    OwnerReplyGraceControlCommand,
    OwnerReplyGraceControlMutation,
    execute_owner_reply_grace_control_command,
    owner_reply_grace_policy_authority,
)
from attention_router.application.owner_response_review_control import (
    OwnerResponseReviewMutation,
    apply_owner_response_review,
)
from attention_router.application.platform.context import resolve_represented_subject
from attention_router.core.events import OperatorAuthority
from attention_router.infrastructure.models import (
    ActorBindingRow,
    InboundEventRow,
    PolicyRow,
    PolicyVersionRow,
)


OWNER_CONTROL_SOURCE_CHANNEL = "wwebjs-owner-control"
OWNER_CONTROL_AUTHENTICATION_MECHANISM = "WWEBJS_AUTHENTICATED_SELF_CHAT"


class OwnerControlError(ValueError):
    pass


class OwnerControlSignalKind(StrEnum):
    COMMAND = "COMMAND"


class OwnerControlAction(StrEnum):
    SET_AUTOMATIC_RESPONSES_ENABLED = "SET_AUTOMATIC_RESPONSES_ENABLED"
    SET_OWNER_REPLY_GRACE_SECONDS = "SET_OWNER_REPLY_GRACE_SECONDS"
    SET_OWNER_REPLY_GRACE_ENABLED = "SET_OWNER_REPLY_GRACE_ENABLED"
    APPROVE_RESPONSE_REVIEW = "APPROVE_RESPONSE_REVIEW"
    REJECT_RESPONSE_REVIEW = "REJECT_RESPONSE_REVIEW"


@dataclass(frozen=True, slots=True)
class GraceSecondsParameters:
    seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.seconds, int) or isinstance(self.seconds, bool):
            raise OwnerControlError("CONTROL_COMMAND_INVALID_VALUE")


@dataclass(frozen=True, slots=True)
class GraceEnabledParameters:
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise OwnerControlError("CONTROL_COMMAND_INVALID_VALUE")


@dataclass(frozen=True, slots=True)
class AutomaticResponsesEnabledParameters:
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise OwnerControlError("CONTROL_COMMAND_INVALID_VALUE")


@dataclass(frozen=True, slots=True)
class ReviewReferenceParameters:
    reference: str

    def __post_init__(self) -> None:
        value = self.reference.strip().casefold()
        allowed = set("0123456789abcdef-")
        if not 8 <= len(value) <= 36 or any(char not in allowed for char in value):
            raise OwnerControlError("OWNER_REVIEW_REFERENCE_INVALID")
        object.__setattr__(self, "reference", value)


OwnerControlParameters = (
    GraceSecondsParameters
    | GraceEnabledParameters
    | AutomaticResponsesEnabledParameters
    | ReviewReferenceParameters
)


@dataclass(frozen=True, slots=True)
class OwnerControlAuthorityEvidence:
    receipt_id: str
    actor_binding_id: str
    authentication_mechanism: str


@dataclass(frozen=True, slots=True)
class OwnerControlSignal:
    signal_id: str
    tenant_id: str
    owner_actor_key: str
    signal_kind: OwnerControlSignalKind
    action: OwnerControlAction
    parameters: OwnerControlParameters
    source_channel: str
    source_event_id: str
    correlation_id: str
    causation_id: str
    occurred_at: datetime
    received_at: datetime
    authority_evidence: OwnerControlAuthorityEvidence

    def __post_init__(self) -> None:
        required = (
            self.signal_id,
            self.tenant_id,
            self.owner_actor_key,
            self.source_channel,
            self.source_event_id,
            self.correlation_id,
            self.causation_id,
        )
        if any(not value.strip() for value in required):
            raise OwnerControlError("OWNER_CONTROL_SIGNAL_INVALID")
        if not isinstance(self.signal_kind, OwnerControlSignalKind):
            raise OwnerControlError("OWNER_CONTROL_SIGNAL_KIND_INVALID")
        if self.signal_kind != OwnerControlSignalKind.COMMAND:
            raise OwnerControlError("OWNER_CONTROL_SIGNAL_KIND_UNSUPPORTED")
        if not isinstance(self.action, OwnerControlAction):
            raise OwnerControlError("OWNER_CONTROL_ACTION_UNSUPPORTED")
        expected = {
            OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED: AutomaticResponsesEnabledParameters,
            OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS: GraceSecondsParameters,
            OwnerControlAction.SET_OWNER_REPLY_GRACE_ENABLED: GraceEnabledParameters,
            OwnerControlAction.APPROVE_RESPONSE_REVIEW: ReviewReferenceParameters,
            OwnerControlAction.REJECT_RESPONSE_REVIEW: ReviewReferenceParameters,
        }[self.action]
        if not isinstance(self.parameters, expected):
            raise OwnerControlError("OWNER_CONTROL_PARAMETERS_INVALID")
        if self.occurred_at.tzinfo is None or self.received_at.tzinfo is None:
            raise OwnerControlError("OWNER_CONTROL_TIMESTAMP_INVALID")


@dataclass(frozen=True, slots=True)
class OwnerControlDispatchResult:
    policy_id: str | None
    mutation: (
        OwnerReplyGraceControlMutation
        | OwnerAutomationControlMutation
        | OwnerResponseReviewMutation
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OwnerControlError("OWNER_CONTROL_TIMESTAMP_INVALID")
    return value.astimezone(timezone.utc)


def _receipt_occurred_at(receipt: InboundEventRow) -> datetime:
    raw = (receipt.payload or {}).get("occurred_at")
    if raw is None:
        return _utc(receipt.received_at)
    if isinstance(raw, datetime):
        return _utc(raw)
    if not isinstance(raw, str):
        raise OwnerControlError("OWNER_CONTROL_TIMESTAMP_INVALID")
    try:
        return _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError as exc:
        raise OwnerControlError("OWNER_CONTROL_TIMESTAMP_INVALID") from exc


def build_owner_operator_authority(
    session: Session,
    *,
    receipt: InboundEventRow,
    binding: ActorBindingRow | None,
) -> tuple[OperatorAuthority, OwnerControlAuthorityEvidence]:
    payload = receipt.payload or {}
    metadata = payload.get("metadata") or {}
    final_classification = metadata.get("final_from_me_classification")
    represented = resolve_represented_subject(session, receipt.tenant_id)
    structurally_authenticated = (
        payload.get("event_origin") == "OWNER_COMMAND"
        and payload.get("owner_authenticated") is True
        and metadata.get("from_me") is True
        and metadata.get("owner_self_chat") is True
        and metadata.get("from_me_classification") == "OWNER_COMMAND"
        and final_classification == "OWNER_COMMAND"
    )
    binding_matches = (
        binding is not None
        and binding.is_active
        and binding.tenant_id == receipt.tenant_id
        and binding.source == receipt.source
        and binding.external_actor_id == payload.get("actor_id")
        and represented is not None
        and binding.actor_key == represented.entity_id
    )
    if not structurally_authenticated or not binding_matches:
        raise OwnerControlError("OWNER_AUTHORITY_UNAVAILABLE")
    authority = OperatorAuthority(
        tenant_id=receipt.tenant_id,
        operator_actor_id=binding.actor_key,
        authenticated=True,
        roles=["OWNER"],
    )
    evidence = OwnerControlAuthorityEvidence(
        receipt_id=receipt.id,
        actor_binding_id=binding.id,
        authentication_mechanism=OWNER_CONTROL_AUTHENTICATION_MECHANISM,
    )
    return authority, evidence


def build_owner_control_signal(
    *,
    receipt: InboundEventRow,
    binding: ActorBindingRow,
    action: OwnerControlAction,
    parameters: OwnerControlParameters,
    authority_evidence: OwnerControlAuthorityEvidence,
) -> OwnerControlSignal:
    return OwnerControlSignal(
        signal_id=receipt.id,
        tenant_id=receipt.tenant_id,
        owner_actor_key=binding.actor_key,
        signal_kind=OwnerControlSignalKind.COMMAND,
        action=action,
        parameters=parameters,
        source_channel=OWNER_CONTROL_SOURCE_CHANNEL,
        source_event_id=receipt.external_event_id,
        correlation_id=receipt.correlation_id,
        causation_id=receipt.id,
        occurred_at=_receipt_occurred_at(receipt),
        received_at=_utc(receipt.received_at),
        authority_evidence=authority_evidence,
    )


def resolve_owner_reply_grace_policy_id(session: Session, *, tenant_id: str) -> str:
    candidates: list[str] = []
    policies = session.scalars(
        select(PolicyRow).where(
            PolicyRow.tenant_id == tenant_id,
            PolicyRow.is_active.is_(True),
        )
    ).all()
    for policy in policies:
        if not policy.current_version_id:
            continue
        version = session.get(PolicyVersionRow, policy.current_version_id)
        if (
            version is not None
            and version.policy_id == policy.identifier
            and version.status == "ACTIVE"
            and owner_reply_grace_policy_authority(version).allowed
        ):
            candidates.append(policy.identifier)
    if not candidates:
        raise OwnerControlError("OWNER_CONTROL_GRACE_POLICY_UNAVAILABLE")
    if len(candidates) != 1:
        raise OwnerControlError("OWNER_CONTROL_GRACE_POLICY_AMBIGUOUS")
    return candidates[0]


def dispatch_owner_control_signal(
    session: Session,
    *,
    signal: OwnerControlSignal,
    authority: OperatorAuthority,
) -> OwnerControlDispatchResult:
    if signal.signal_kind != OwnerControlSignalKind.COMMAND:
        raise OwnerControlError("OWNER_CONTROL_SIGNAL_KIND_UNSUPPORTED")
    if signal.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED:
        if not isinstance(signal.parameters, AutomaticResponsesEnabledParameters):
            raise OwnerControlError("OWNER_CONTROL_PARAMETERS_INVALID")
        mutation = set_automatic_responses_enabled(
            session, tenant_id=signal.tenant_id,
            represented_owner_actor_key=signal.owner_actor_key,
            enabled=signal.parameters.enabled, authority=authority,
            source_channel=signal.source_channel, source_event_id=signal.source_event_id,
            provenance={
                "action": signal.action.value,
                "receipt_id": signal.authority_evidence.receipt_id,
                "actor_binding_id": signal.authority_evidence.actor_binding_id,
                "authentication_mechanism": signal.authority_evidence.authentication_mechanism,
            },
        )
        return OwnerControlDispatchResult(policy_id=None, mutation=mutation)
    if signal.action in {
        OwnerControlAction.APPROVE_RESPONSE_REVIEW,
        OwnerControlAction.REJECT_RESPONSE_REVIEW,
    }:
        if not isinstance(signal.parameters, ReviewReferenceParameters):
            raise OwnerControlError("OWNER_CONTROL_PARAMETERS_INVALID")
        mutation = apply_owner_response_review(
            session,
            tenant_id=signal.tenant_id,
            reference=signal.parameters.reference,
            approve=signal.action == OwnerControlAction.APPROVE_RESPONSE_REVIEW,
            authority=authority,
        )
        return OwnerControlDispatchResult(policy_id=None, mutation=mutation)
    # Owner Reply Grace is a single owner-scoped contract, not a policy selector.
    policy_id = None
    if signal.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_SECONDS:
        if not isinstance(signal.parameters, GraceSecondsParameters):
            raise OwnerControlError("OWNER_CONTROL_PARAMETERS_INVALID")
        command_type = OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_SECONDS
        value: int | bool = signal.parameters.seconds
    elif signal.action == OwnerControlAction.SET_OWNER_REPLY_GRACE_ENABLED:
        if not isinstance(signal.parameters, GraceEnabledParameters):
            raise OwnerControlError("OWNER_CONTROL_PARAMETERS_INVALID")
        command_type = OwnerReplyGraceCommandType.SET_OWNER_REPLY_GRACE_ENABLED
        value = signal.parameters.enabled
    else:
        raise OwnerControlError("OWNER_CONTROL_ACTION_UNSUPPORTED")
    mutation = execute_owner_reply_grace_control_command(
        session,
        tenant_id=signal.tenant_id,
        command=OwnerReplyGraceControlCommand(
            command_id=signal.signal_id,
            command_type=command_type,
            value=value,
            policy_id=policy_id,
            source_channel=signal.source_channel,
        ),
        authority=authority,
    )
    return OwnerControlDispatchResult(policy_id=policy_id, mutation=mutation)


__all__ = [
    "AutomaticResponsesEnabledParameters",
    "GraceEnabledParameters",
    "GraceSecondsParameters",
    "ReviewReferenceParameters",
    "OwnerControlAction",
    "OwnerControlAuthorityEvidence",
    "OwnerControlDispatchResult",
    "OwnerControlError",
    "OwnerControlSignal",
    "OwnerControlSignalKind",
    "build_owner_control_signal",
    "build_owner_operator_authority",
    "dispatch_owner_control_signal",
    "resolve_owner_reply_grace_policy_id",
]
