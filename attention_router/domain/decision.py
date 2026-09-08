from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from attention_router.domain.behavior import explicit_intent, render_response


class DecisionType(StrEnum):
    RESPOND = "RESPOND"
    DO_NOT_RESPOND = "DO_NOT_RESPOND"
    ESCALATE = "ESCALATE"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    NO_POLICY = "NO_POLICY"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


@dataclass(frozen=True)
class DecisionContext:
    event_id: str
    interaction_id: str
    channel: str
    source: str
    actor_id: str | None
    actor_binding_id: str | None
    actor_category: str
    actor_metadata: dict
    agent_blueprint_id: str | None
    agent_blueprint_version: int | None
    agent_spec: dict | None
    audience: str | None
    policy_id: str | None
    policy_version_id: str | None
    policy_config: dict | None
    autonomy: str
    inbound_text: str = ""


@dataclass(frozen=True)
class DecisionResult:
    decision_type: DecisionType
    recommended_action: str
    proposed_response: str | None
    escalation_required: bool
    escalation_reason: str | None
    confidence: float
    missing_information: list[str]
    reasoning_summary: str
    execution_allowed: bool
    external_delivery_allowed: bool
    intent: str | None = None
    objective: str | None = None
    self_contained: bool = False
    context_sufficient: bool = False
    context_requirements: list[str] | None = None


class DecisionEngine:
    def decide(self, context: DecisionContext) -> DecisionResult:
        if not context.agent_blueprint_id:
            return DecisionResult(
                DecisionType.INSUFFICIENT_CONTEXT,
                "observe",
                None,
                False,
                None,
                0.2,
                ["agent_blueprint"],
                "Nenhum agent blueprint configurado para este contexto.",
                False,
                False,
            )
        if not context.policy_id:
            return DecisionResult(
                DecisionType.NO_POLICY,
                "observe",
                None,
                False,
                None,
                0.2,
                ["policy"],
                "Nenhuma policy aplicavel foi resolvida.",
                False,
                False,
            )
        spec = context.agent_spec or {}
        intent = explicit_intent(context.inbound_text) if context.inbound_text else None
        # Identity questions are complete by themselves; blueprint
        # completeness must not turn them into a generic context request.
        self_contained = intent in {"IDENTITY_QUESTION", "ASSISTANT_NATURE_QUESTION"}
        objective = {
            "IDENTITY_QUESTION": "identity/name",
            "ASSISTANT_NATURE_QUESTION": "identity/transparency",
        }.get(intent)
        context_requirements = list(spec.get("missing_information") or [])
        missing = [] if self_contained else context_requirements
        if missing:
            return DecisionResult(
                DecisionType.INSUFFICIENT_CONTEXT,
                "request_information",
                "Pode me passar mais algumas informações para eu ajudar?",
                False,
                None,
                0.55,
                missing,
                "O blueprint possui informacoes faltantes; nenhuma execucao e permitida.",
                False,
                False,
                intent,
                objective,
                self_contained,
                False,
                context_requirements,
            )
        escalation = bool(spec.get("escalation"))
        action = "respond" if self_contained else (context.policy_config or {}).get("allowed_actions", ["respond"])[0]
        decision_type = DecisionType.ESCALATE if escalation else DecisionType.RESPOND
        reason = (
            "Intento de identidade autocontido; nenhuma informacao adicional e necessaria."
            if self_contained
            else "Decision deterministica baseada no blueprint, audience, policy e autonomia."
        )
        return DecisionResult(
            decision_type,
            action,
            "Resposta proposta para revisao humana.",
            escalation,
            "Blueprint exige escalonamento." if escalation else None,
            0.8,
            [],
            reason,
            False,
            False,
            intent,
            objective,
            self_contained,
            True,
            context_requirements,
        )


class ResponseGenerator:
    def propose(
        self,
        result: DecisionResult,
        *,
        behavior: dict[str, Any] | None = None,
        audience: str | None = None,
        introduced: bool = False,
        recent_variant_ids: set[str] | None = None,
        urgent: bool = False,
    ) -> str | None:
        if behavior is None:
            return result.proposed_response
        candidate = render_response(
            result,
            behavior,
            audience=audience,
            introduced=introduced,
            recent_variant_ids=recent_variant_ids,
            urgent=urgent,
        )
        return candidate.text if candidate else None
