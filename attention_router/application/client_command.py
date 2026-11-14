"""Authenticated provider-neutral Andy command channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from attention_router.application.owner_control_clarification import (
    OwnerClarificationResolutionKind,
    OwnerControlClarificationError,
    render_owner_control_clarification,
    render_owner_control_clarification_canceled,
    render_owner_control_clarification_unavailable,
    resolve_owner_control_clarification_reply,
    selected_candidate,
)
from attention_router.application.owner_control_semantic_candidates import (
    OwnerControlCandidateBuilderError,
    interpret_owner_control_candidates,
)
from attention_router.application.owner_control_semantic_registry import (
    materialize_owner_control_candidate,
    OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
    build_registered_semantic_candidate,
)
from attention_router.application.pending_intent import (
    PendingIntentError,
    cancel_pending_intent,
    create_pending_intent,
    build_candidate_set,
    expire_due_pending_intents,
    find_active_pending_intent,
    resolve_pending_intent,
    supersede_pending_intent,
)
from attention_router.application.user_idiolect import (
    retrieve_idiolect_interpretation_evidence,
    select_unique_confirmed_candidate,
    select_unique_confirmed_mapping,
)
from attention_router.application.user_idiolect_projection import (
    IdiolectProjectionError,
    project_resolved_pending_intent_language_fact,
)
from attention_router.application.owner_operational_control import (
    OperationalControlConflict,
    OperationalControlError,
    OperationalControlUnauthorized,
)
from attention_router.application.platform.context import resolve_represented_subject
from attention_router.application.speech_transcription import (
    ALLOWED_SPEECH_MIME_TYPES,
    InternalSpeechTranscriber,
    SpeechTranscriptionError,
)
from attention_router.config import Settings
from attention_router.core.client.bootstrap import TenantRole
from attention_router.core.events import OperatorAuthority, OwnerCommandUnauthorized
from attention_router.infrastructure import client_session_repository
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ClientCommandMessageRow,
    OwnerAutomationControlRow,
)
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


class ClientCommandVoiceDisabled(ClientCommandError):
    code = "CLIENT_COMMAND_VOICE_DISABLED"


class ClientCommandVoiceInvalid(ClientCommandError):
    code = "CLIENT_COMMAND_VOICE_INVALID"


class ClientCommandVoiceUnavailable(ClientCommandError):
    code = "CLIENT_COMMAND_VOICE_UNAVAILABLE"


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


def _automation_interpretation_state(
    session: Session,
    *,
    tenant_id: str,
    owner_actor_key: str,
) -> str:
    """Read only the authenticated owner's persisted automation control."""
    enabled = session.scalar(
        select(OwnerAutomationControlRow.automatic_responses_enabled).where(
            OwnerAutomationControlRow.tenant_id == tenant_id,
            OwnerAutomationControlRow.represented_owner_actor_key == owner_actor_key,
        )
    )
    if enabled is False:
        return "PAUSED"
    if enabled is True:
        return "ACTIVE"
    return "UNKNOWN"


_IMPLICIT_COMMAND_BLOCKERS = re.compile(
    r"\b(?:nao|nunca|jamais|talvez|se|quando|imagine|"
    r"exemplo|hipotese|suponha|simule)\b"
)


def _safe_implicit_command_text(text: str) -> bool:
    """Fail closed on quoted, interrogative, hypothetical or negated text."""
    if not isinstance(text, str) or not text.strip():
        return False
    if any(char in text for char in ('?', '"', "'", '“', '”', '«', '»', '\n')):
        return False
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    ascii_text = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return _IMPLICIT_COMMAND_BLOCKERS.search(ascii_text) is None


def _explicit_automation_command(text: str) -> bool:
    fast = _client_fast_path(text)
    if fast is not None and fast.status == OwnerControlParseStatus.MATCHED:
        return True
    direct = parse_owner_grace_control(text)
    if direct.status == OwnerControlParseStatus.MATCHED:
        return True
    normalized = _normalized_phrase(text)
    return "respostas automaticas" in normalized or "resposta automatica" in normalized


class ClientCommandService:
    def __init__(
        self,
        *,
        settings: Settings,
        client_sessions: ClientSessionService,
        speech_transcriber: InternalSpeechTranscriber | None = None,
    ):
        self.settings = settings
        self.client_sessions = client_sessions
        self.speech_transcriber = speech_transcriber or InternalSpeechTranscriber(
            settings=settings
        )

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

    @staticmethod
    def _valid_request_id(client_request_id: str) -> bool:
        return bool(
            client_request_id
            and len(client_request_id) <= 80
            and re.fullmatch(r"[A-Za-z0-9_.:-]+", client_request_id)
        )

    @staticmethod
    def _valid_text(text: object) -> str:
        stripped = text.strip() if isinstance(text, str) else ""
        if not stripped or len(stripped) > 4000:
            raise ClientCommandInvalid()
        return stripped

    def _submit_with_authority(
        self,
        session: Session,
        *,
        bootstrap,
        session_row,
        operational_tenant_id: str,
        owner_actor_key: str,
        operator: OperatorAuthority,
        client_request_id: str,
        text: str,
        modality: str,
        current: datetime,
    ) -> ClientCommandView:

        row = ClientCommandMessageRow(
            id=str(uuid4()),
            tenant_id=bootstrap.active_tenant_id,
            human_identity_id=bootstrap.human_identity_id,
            device_id=bootstrap.device.device_id,
            client_session_id=session_row.id,
            client_request_id=client_request_id,
            modality=modality,
            input_text=text,
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
                and replay.modality == modality
                and replay.input_text == text
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

        conversation_key_hash = stable_hash(
            f"{CLIENT_COMMAND_SOURCE_CHANNEL}:{bootstrap.human_identity_id}"
        )
        expire_due_pending_intents(session, timestamp=current, limit=100)
        active_pending = find_active_pending_intent(
            session,
            tenant_id=operational_tenant_id,
            represented_owner_actor_key=owner_actor_key,
            source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
            conversation_key_hash=conversation_key_hash,
            timestamp=current,
        )

        parsed: OwnerControlParseResult
        if active_pending is not None:
            try:
                resolution = resolve_owner_control_clarification_reply(
                    text,
                    active_pending.candidate_set,
                )
            except OwnerControlClarificationError:
                resolution = None

            if (
                resolution is not None
                and resolution.kind == OwnerClarificationResolutionKind.CANCEL
            ):
                cancel_pending_intent(
                    session,
                    pending_intent_id=active_pending.id,
                    reason="OWNER_REJECTED_CLARIFICATION",
                    resolution_client_command_id=row.id,
                    timestamp=current,
                )
                row.state = "COMPLETED"
                row.response_text = render_owner_control_clarification_canceled()
                row.processed_at = current
                session.flush()
                return self._view(row)

            if (
                resolution is not None
                and resolution.kind == OwnerClarificationResolutionKind.SELECT
            ):
                assert resolution.candidate_key is not None
                try:
                    resolve_pending_intent(
                        session,
                        pending_intent_id=active_pending.id,
                        tenant_id=operational_tenant_id,
                        represented_owner_actor_key=owner_actor_key,
                        source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
                        conversation_key_hash=conversation_key_hash,
                        selected_candidate_key=resolution.candidate_key,
                        resolution_kind="EXPLICIT_OPTION_SELECTION",
                        resolution_client_command_id=row.id,
                        timestamp=current,
                    )
                    candidate = selected_candidate(
                        active_pending.candidate_set,
                        resolution.candidate_key,
                    )
                except (PendingIntentError, OwnerControlClarificationError):
                    row.state = "CLARIFICATION_REQUIRED"
                    row.error_code = "CONTROL_COMMAND_NEEDS_CLARIFICATION"
                    row.response_text = render_owner_control_clarification(
                        active_pending.candidate_set
                    )
                    row.processed_at = current
                    session.flush()
                    return self._view(row)

                try:
                    project_resolved_pending_intent_language_fact(
                        session,
                        pending_intent_id=active_pending.id,
                    )
                except IdiolectProjectionError as exc:
                    audit(
                        session,
                        None,
                        "idiolect.confirmed_projection_failed",
                        {
                            "pending_intent_id": active_pending.id,
                            "error_code": str(exc),
                        },
                        correlation_id=row.id,
                        causation_id=row.id,
                        origin="user_idiolect",
                        tenant_id=operational_tenant_id,
                        created_at=current,
                    )

                materialized = materialize_owner_control_candidate(
                    intent_key=str(candidate["semantic_intent_key"]),
                    parameters=dict(candidate["parameters"]),
                )
                if materialized is None:
                    row.state = "COMPLETED"
                    row.response_text = render_owner_control_clarification_unavailable(
                        candidate
                    )
                    row.processed_at = current
                    session.flush()
                    return self._view(row)
                action, parameters = materialized
                parsed = OwnerControlParseResult(
                    OwnerControlParseStatus.MATCHED,
                    action,
                    parameters,
                )
            else:
                explicit = self._interpret(text)
                if (
                    explicit.status == OwnerControlParseStatus.MATCHED
                    and (
                        explicit.action != OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
                        or _explicit_automation_command(text)
                    )
                ):
                    supersede_pending_intent(
                        session,
                        pending_intent_id=active_pending.id,
                        superseding_source_client_command_id=row.id,
                        reason="NEWER_EXPLICIT_COMMAND",
                        timestamp=current,
                    )
                    parsed = explicit
                else:
                    row.state = "CLARIFICATION_REQUIRED"
                    row.error_code = "CONTROL_COMMAND_NEEDS_CLARIFICATION"
                    row.response_text = render_owner_control_clarification(
                        active_pending.candidate_set
                    )
                    row.processed_at = current
                    session.flush()
                    return self._view(row)
        else:
            parsed = None

        confirmed_idiolect_used = False
        owner_automation_state = _automation_interpretation_state(
            session,
            tenant_id=operational_tenant_id,
            owner_actor_key=owner_actor_key,
        )
        if active_pending is None:
            # A confirmed owner phrase can be interpreted without another model
            # call. This does not bypass the authenticated owner dispatcher.
            if (
                self.settings.owner_control_semantic_enabled
                and owner_automation_state == "PAUSED"
                and _safe_implicit_command_text(text)
                and not _explicit_automation_command(text)
            ):
                try:
                    evidence = retrieve_idiolect_interpretation_evidence(
                        session,
                        tenant_id=operational_tenant_id,
                        actor_key=owner_actor_key,
                        utterance=text,
                        source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
                        conversation_key_hash=conversation_key_hash,
                        now=current,
                    )
                    learned = select_unique_confirmed_mapping(evidence)
                    if (
                        learned is not None
                        and learned.semantic_intent_key == "SET_AUTOMATIC_RESPONSES"
                        and learned.parameters == {"enabled": True}
                    ):
                        mapped = materialize_owner_control_candidate(
                            intent_key=learned.semantic_intent_key,
                            parameters=learned.parameters,
                        )
                        if mapped is not None:
                            parsed = OwnerControlParseResult(
                                OwnerControlParseStatus.MATCHED,
                                mapped[0],
                                mapped[1],
                            )
                            confirmed_idiolect_used = True
                            audit(
                                session,
                                None,
                                "client_command.confirmed_idiolect_matched",
                                {
                                    "command_id": row.id,
                                    "language_fact_id": learned.fact_id,
                                    "owner_automation_state": owner_automation_state,
                                },
                                correlation_id=row.id,
                                causation_id=row.id,
                                origin="user_idiolect",
                                tenant_id=row.tenant_id,
                                created_at=current,
                            )
                except Exception:
                    # A failed or conflicting language fact is not an authority
                    # source. Continue through the normal semantic path.
                    parsed = None
            if parsed is None:
                # Unknown metaphorical owner language enters clarification
                # directly when automation is paused. This is one model
                # candidate pass instead of speculative execution + a second
                # LLM call, and never treats an idiom as canonical authority.
                deterministic = (
                    _client_fast_path(text) or parse_owner_grace_control(text)
                )
                if (
                    self.settings.owner_control_semantic_enabled
                    and owner_automation_state == "PAUSED"
                    and deterministic.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND
                    and _safe_implicit_command_text(text)
                    and not _explicit_automation_command(text)
                ):
                    parsed = deterministic
                else:
                    parsed = self._interpret(text)

        candidate_set: dict[str, object] | None = None
        # Inferential phrases must not auto-toggle automation on first use.
        # Canonical commands remain immediate. Only a plausible state-specific
        # semantic inference may be offered as a confirmable candidate.
        if (
            active_pending is None
            and parsed.status == OwnerControlParseStatus.MATCHED
            and parsed.action == OwnerControlAction.SET_AUTOMATIC_RESPONSES_ENABLED
            and isinstance(parsed.parameters, AutomaticResponsesEnabledParameters)
            and not _explicit_automation_command(text)
            and not confirmed_idiolect_used
        ):
            enabled = parsed.parameters.enabled
            plausible = (
                (owner_automation_state == "PAUSED" and enabled is True)
                or (owner_automation_state == "ACTIVE" and enabled is False)
            )
            if plausible and _safe_implicit_command_text(text):
                candidate = build_registered_semantic_candidate(
                    intent_key="SET_AUTOMATIC_RESPONSES",
                    parameters={"enabled": enabled},
                    confidence="medium",
                )
                candidate_set = build_candidate_set(
                    [candidate],
                    semantic_registry_version=OWNER_CONTROL_SEMANTIC_REGISTRY_VERSION,
                )
                parsed = OwnerControlParseResult(
                    OwnerControlParseStatus.REJECTED,
                    reason_code="CONTROL_COMMAND_NEEDS_CLARIFICATION",
                )
            else:
                parsed = OwnerControlParseResult(
                    OwnerControlParseStatus.NOT_CONTROL_COMMAND,
                )
        should_build_candidates = (
            (
                parsed.status == OwnerControlParseStatus.REJECTED
                and parsed.reason_code == "CONTROL_COMMAND_NEEDS_CLARIFICATION"
            )
            or (
                parsed.status == OwnerControlParseStatus.NOT_CONTROL_COMMAND
                and self.settings.owner_control_semantic_enabled
            )
        )
        if (
            should_build_candidates
            and candidate_set is None
            and _safe_implicit_command_text(text)
        ):
            try:
                candidate_set = interpret_owner_control_candidates(
                    text, owner_automation_state=owner_automation_state
                )
            except OwnerControlCandidateBuilderError:
                candidate_set = None
            # An unanchored metaphor is not a command when the automation
            # status cannot support its proposed meaning.
            if candidate_set is not None and not _explicit_automation_command(text):
                suggested = candidate_set.get("candidates", [])
                if (
                    owner_automation_state != "PAUSED"
                    and any(
                        item.get("semantic_intent_key") == "SET_AUTOMATIC_RESPONSES"
                        and (item.get("parameters") or {}).get("enabled") is True
                        for item in suggested if isinstance(item, dict)
                    )
                ):
                    candidate_set = None
            if candidate_set is not None:
                try:
                    evidence = retrieve_idiolect_interpretation_evidence(
                        session,
                        tenant_id=operational_tenant_id,
                        actor_key=owner_actor_key,
                        utterance=text,
                        source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
                        conversation_key_hash=conversation_key_hash,
                        now=current,
                    )
                except Exception:
                    evidence = ()
                learned = select_unique_confirmed_candidate(
                    evidence,
                    candidate_set,
                )
                if learned is not None:
                    materialized = materialize_owner_control_candidate(
                        intent_key=str(learned["semantic_intent_key"]),
                        parameters=dict(learned["parameters"]),
                    )
                    if materialized is not None:
                        action, parameters = materialized
                        parsed = OwnerControlParseResult(
                            OwnerControlParseStatus.MATCHED,
                            action,
                            parameters,
                        )

            if (
                parsed.status
                in {
                    OwnerControlParseStatus.REJECTED,
                    OwnerControlParseStatus.NOT_CONTROL_COMMAND,
                }
                and candidate_set is not None
            ):
                try:
                    pending = create_pending_intent(
                        session,
                        tenant_id=operational_tenant_id,
                        represented_owner_actor_key=owner_actor_key,
                        source_tenant_id=row.tenant_id,
                        source_channel=CLIENT_COMMAND_SOURCE_CHANNEL,
                        conversation_key_hash=conversation_key_hash,
                        semantic_registry_version=str(
                            candidate_set["semantic_registry_version"]
                        ),
                        ambiguity_reason="CONTROL_COMMAND_NEEDS_CLARIFICATION",
                        candidates=list(candidate_set["candidates"]),
                        expires_at=current
                        + timedelta(
                            seconds=self.settings.owner_control_clarification_ttl_seconds
                        ),
                        source_client_command_id=row.id,
                        provenance={
                            "normalization_source": "SEMANTIC_AI",
                            "candidate_builder": "owner_control_v1b",
                            "client_command_id": row.id,
                        },
                        timestamp=current,
                    )
                    prompt = render_owner_control_clarification(
                        pending.candidate_set
                    )
                except (PendingIntentError, OwnerControlClarificationError):
                    pass
                else:
                    row.state = "CLARIFICATION_REQUIRED"
                    row.error_code = "CONTROL_COMMAND_NEEDS_CLARIFICATION"
                    row.response_text = prompt
                    row.processed_at = current
                    audit(
                        session,
                        None,
                        "client_command.clarification_requested",
                        {
                            "command_id": row.id,
                            "pending_intent_id": pending.id,
                            "candidate_count": len(
                                pending.candidate_set["candidates"]
                            ),
                        },
                        correlation_id=row.id,
                        causation_id=row.id,
                        origin="client_command",
                        tenant_id=row.tenant_id,
                        created_at=current,
                    )
                    session.flush()
                    return self._view(row)
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
                    "modality": modality,
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
                "modality": modality,
            },
            correlation_id=row.id,
            causation_id=row.id,
            origin="client_command",
            tenant_id=row.tenant_id,
            created_at=current,
        )
        session.flush()
        return self._view(row)
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
        if not self._valid_request_id(client_request_id):
            raise ClientCommandInvalid()
        stripped = self._valid_text(text)

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

        return self._submit_with_authority(
            session,
            bootstrap=bootstrap,
            session_row=session_row,
            operational_tenant_id=operational_tenant_id,
            owner_actor_key=owner_actor_key,
            operator=operator,
            client_request_id=client_request_id,
            text=stripped,
            modality="TEXT",
            current=current,
        )

    def submit_voice(
        self,
        session: Session,
        *,
        session_token: str | None,
        client_request_id: str,
        audio: bytes,
        mime_type: str,
        now: datetime | None = None,
    ) -> ClientCommandView:
        self._require_enabled()
        if not self.settings.client_command_voice_enabled:
            raise ClientCommandVoiceDisabled()
        current = now or datetime.now(UTC)
        if not self._valid_request_id(client_request_id):
            raise ClientCommandVoiceInvalid()
        normalized_mime = mime_type.split(";", 1)[0].strip().casefold()
        if (
            normalized_mime not in ALLOWED_SPEECH_MIME_TYPES
            or not audio
            or len(audio) > self.settings.client_command_voice_max_bytes
        ):
            raise ClientCommandVoiceInvalid()

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
            if existing.modality == "VOICE":
                return self._view(existing)
            raise ClientCommandConflict()

        try:
            transcription = self.speech_transcriber.transcribe_bytes(
                audio,
                mime_type=normalized_mime,
                max_bytes=self.settings.client_command_voice_max_bytes,
            )
        except SpeechTranscriptionError as exc:
            audit(
                session,
                None,
                "client_command.voice_transcription_failed",
                {"error_code": str(exc)[:120]},
                correlation_id=client_request_id,
                causation_id=None,
                origin="client_command",
                tenant_id=bootstrap.active_tenant_id,
                created_at=current,
            )
            raise ClientCommandVoiceUnavailable() from exc

        transcript = self._valid_text(transcription.transcript)
        audit(
            session,
            None,
            "client_command.voice_transcribed",
            {
                "provider": transcription.provider,
                "model": transcription.model,
                "device_id": bootstrap.device.device_id,
            },
            correlation_id=client_request_id,
            causation_id=None,
            origin="client_command",
            tenant_id=bootstrap.active_tenant_id,
            created_at=current,
        )
        return self._submit_with_authority(
            session,
            bootstrap=bootstrap,
            session_row=session_row,
            operational_tenant_id=operational_tenant_id,
            owner_actor_key=owner_actor_key,
            operator=operator,
            client_request_id=client_request_id,
            text=transcript,
            modality="VOICE",
            current=current,
        )

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
    "ClientCommandVoiceDisabled",
    "ClientCommandVoiceInvalid",
    "ClientCommandVoiceUnavailable",
    "ClientCommandService",
    "ClientCommandView",
]
