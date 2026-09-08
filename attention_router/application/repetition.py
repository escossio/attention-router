"""Deterministic conversation-level suppression for low-information repeats."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.behavior import explicit_intent, response_objective as behavior_response_objective
from attention_router.domain.models import now_utc
from attention_router.config import settings
from attention_router.application.direct_conversation import direct_conversation_eligibility
from attention_router.infrastructure.models import AgentDecisionRow, InteractionRow, OutboxMessageRow
from attention_router.infrastructure.repository import audit


_BOILERPLATE = {
    "pode", "me", "chamar", "chama", "de", "por", "favor", "prefiro", "que", "m", "chame",
    "preciso", "falar", "com", "ele", "ela", "isso", "isto", "sobre", "assunto",
    "informacao", "informacoes", "explicar", "melhor", "consegue", "passar", "mais",
    "alguma", "algumas", "para", "eu", "ajudar", "bom", "dia", "oi", "ola",
}


@dataclass(frozen=True)
class ConversationStateSignature:
    actor_id: str
    intent: str
    objective: str
    semantic_input: str

    @property
    def value(self) -> str:
        return "|".join((self.actor_id, self.intent, self.objective, self.semantic_input))

    @property
    def state_value(self) -> str:
        return "|".join((self.actor_id, self.objective, self.semantic_input))


@dataclass(frozen=True)
class RepetitionDecision:
    suppress: bool
    reason: str | None
    signature: str
    previous_decision_id: str | None = None
    previous_response_hash: str | None = None
    response_objective: str | None = None
    objective_already_satisfied: bool = False
    previous_useful_response_generated: bool = False
    previous_useful_response_delivered: bool = False


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _semantic_input(text: str) -> str:
    folded = _fold(text)
    folded = re.sub(r"[^\w\s]", " ", folded)
    tokens = [token for token in folded.split() if token not in _BOILERPLATE and len(token) > 1]
    return " ".join(sorted(set(tokens)))


def _response_objective(text: str) -> str:
    folded = _fold(text)
    if any(phrase in folded for phrase in ("qual e o seu nome", "como voce se chama")):
        return "identity/name"
    if "quem esta falando" in folded:
        return "identity/who"
    if "voce e uma ia" in folded or "voce e um robo" in folded:
        return "identity/ai"
    if any(phrase in folded for phrase in ("o alex esta ai", "o alex esta aí")):
        return "ANSWER_AVAILABILITY"
    return behavior_response_objective(None, text, None)


def state_signature(actor_id: str, text: str, recommended_action: str | None = None) -> ConversationStateSignature:
    intent = explicit_intent(text)
    objective = _response_objective(text)
    semantic_input = "" if objective in {"identity/name", "identity/who", "identity/ai", "ANSWER_AVAILABILITY"} else _semantic_input(text)
    return ConversationStateSignature(actor_id, intent, objective, semantic_input)


def response_signature(decision: AgentDecisionRow, interaction: InteractionRow) -> ConversationStateSignature:
    signature = state_signature(interaction.contact_id, interaction.inbound_text, decision.recommended_action)
    return ConversationStateSignature(
        signature.actor_id,
        signature.intent,
        decision.objective or signature.objective,
        signature.semantic_input,
    )


def _has_prior_outbound(session: Session, interaction_id: str) -> bool:
    return session.scalar(select(OutboxMessageRow.id).where(
        OutboxMessageRow.interaction_id == interaction_id,
        OutboxMessageRow.status == "DONE",
    ).limit(1)) is not None


def _useful_response(objective: str, text: str | None) -> bool:
    folded = _fold(text or "")
    if objective not in {"identity/name", "identity/who", "identity/ai", "ANSWER_AVAILABILITY"}:
        return True
    if not folded:
        return False
    if objective in {"identity/name", "identity/who", "identity/ai"}:
        return any(marker in folded for marker in ("andy", "assistente", "meu nome", "sou uma ia"))
    if objective == "ANSWER_AVAILABILITY":
        return "alex" in folded or "não" in folded or "nao" in folded
    return True


def check_text_repetition(session: Session, interaction: InteractionRow) -> RepetitionDecision:
    current = state_signature(interaction.contact_id, interaction.inbound_text)
    window_start = interaction.created_at - timedelta(seconds=settings.conversation_repetition_window_seconds)
    previous_interactions = session.scalars(select(InteractionRow).join(
        OutboxMessageRow, OutboxMessageRow.interaction_id == InteractionRow.id,
    ).where(
        InteractionRow.tenant_id == interaction.tenant_id,
        InteractionRow.contact_id == interaction.contact_id,
        InteractionRow.created_at < interaction.created_at,
        InteractionRow.created_at >= window_start,
        OutboxMessageRow.status == "DONE",
    ).distinct().order_by(InteractionRow.created_at.desc()).limit(20)).all()
    for previous in previous_interactions:
        if previous.active_context != interaction.active_context and (previous.active_context or interaction.active_context):
            continue
        if _has_prior_outbound(session, previous.id):
            prior = state_signature(previous.contact_id, previous.inbound_text)
            outbound = session.scalar(select(OutboxMessageRow).where(
                OutboxMessageRow.interaction_id == previous.id, OutboxMessageRow.status == "DONE"
            ).order_by(OutboxMessageRow.completed_at.desc()).limit(1))
            if prior.state_value == current.state_value and _useful_response(current.objective, (outbound.payload or {}).get("text")):
                return RepetitionDecision(
                    True, "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED", current.state_value,
                    response_objective=current.objective, objective_already_satisfied=True,
                    previous_useful_response_generated=True, previous_useful_response_delivered=True,
                )
    return RepetitionDecision(False, None, current.state_value, response_objective=current.objective)


def _previous_response(session: Session, interaction_id: str, objective: str) -> tuple[AgentDecisionRow, str] | None:
    decision = session.scalar(select(AgentDecisionRow).where(AgentDecisionRow.interaction_id == interaction_id))
    if decision is None:
        return None
    if not _has_prior_outbound(session, interaction_id):
        return None
    response = decision.proposed_response or ""
    if not _useful_response(objective, response):
        return None
    return decision, sha256(_fold(response).encode("utf-8")).hexdigest()[:16]


def check_repetition(session: Session, decision: AgentDecisionRow, interaction: InteractionRow) -> RepetitionDecision:
    current = response_signature(decision, interaction)
    window_start = interaction.created_at - timedelta(seconds=settings.conversation_repetition_window_seconds)
    previous_interactions = session.scalars(select(InteractionRow).join(
        OutboxMessageRow, OutboxMessageRow.interaction_id == InteractionRow.id,
    ).where(
        InteractionRow.tenant_id == interaction.tenant_id,
        InteractionRow.contact_id == interaction.contact_id,
        InteractionRow.created_at < interaction.created_at,
        InteractionRow.created_at >= window_start,
        OutboxMessageRow.status == "DONE",
    ).distinct().order_by(InteractionRow.created_at.desc()).limit(20)).all()
    for previous_interaction in previous_interactions:
        if previous_interaction.active_context != interaction.active_context and (
            previous_interaction.active_context or interaction.active_context
        ):
            continue
        previous = _previous_response(session, previous_interaction.id, current.objective)
        if previous is None:
            continue
        previous_decision, previous_text = previous
        prior_signature = response_signature(previous_decision, previous_interaction)
        if prior_signature == current:
            return RepetitionDecision(
                True, "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED", current.value, previous_decision.id, previous_text,
                current.objective, True, bool(previous_decision.proposed_response), True,
            )
    return RepetitionDecision(False, None, current.value, response_objective=current.objective)


def suppress_if_repeated(
    session: Session,
    decision: AgentDecisionRow,
    interaction: InteractionRow,
    event=None,
) -> RepetitionDecision:
    """Suppress autonomous/legacy repeats, never a fresh eligible user turn.

    Event and decision idempotency are enforced by the inbound/decision/intent/
    outbox keys.  This guard is therefore a conversational safety policy, not
    the duplicate-processing mechanism.  A direct organic inbound that reached
    the Agent is a new user turn and must be allowed to proceed even when its
    objective resembles a previously delivered answer.
    """
    if event is not None and direct_conversation_eligibility(event).eligible:
        current = response_signature(decision, interaction)
        return RepetitionDecision(False, None, current.value, response_objective=current.objective)
    result = check_repetition(session, decision, interaction)
    if result.suppress:
        audit(
            session,
            interaction.id,
            "response_suppressed",
            {
                "reason": "SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED",
                "response_objective": result.response_objective,
                "objective_already_satisfied": result.objective_already_satisfied,
                "previous_useful_response_generated": result.previous_useful_response_generated,
                "previous_useful_response_delivered": result.previous_useful_response_delivered,
                "semantic_repeat": result.suppress,
                "state_changed": False,
                "semantic_response_signature": result.signature,
                "previous_decision_id": result.previous_decision_id,
                "previous_response_hash": result.previous_response_hash,
                "metrics": [
                    "responses_suppressed_repeat_total",
                    "semantic_repeat_inbounds_total",
                    "low_information_loop_suppressed_total",
                ],
                "timestamp": now_utc().isoformat(),
            },
            origin="conversation_repetition_guard",
        )
    return result
