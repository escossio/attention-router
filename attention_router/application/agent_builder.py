import copy
import time
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.ai_interviewer import (
    AI_INTERVIEWER_PROMPT_VERSION,
    AIInterviewerError,
    OpenAIConfigurationInterviewer,
)
from attention_router.config import settings
from attention_router.domain.agent_builder import (
    AgentBlueprintSpec,
    AutonomyLevel,
    BlueprintStatus,
    BlueprintPatch,
    BlueprintPatchOperation,
    ConfigurationSessionStatus,
    InterviewResult,
    Question,
    RequirementEvidenceCoverage,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentBlueprintRow,
    AgentBlueprintVersionRow,
    AuditEventRow,
    ConfigurationSessionRow,
)
from attention_router.infrastructure.repository import audit


STAGES = [
    "objective",
    "audience",
    "context",
    "tone",
    "goals",
    "knowledge",
    "allowed_actions",
    "approval_boundaries",
    "forbidden_actions",
    "escalation",
    "autonomy",
    "review",
]

CANONICAL_REQUIREMENTS_KEY = "_requirements"
REQUIREMENT_EVIDENCE_KEY = "_requirement_evidence"
REQUIREMENT_EVIDENCE_AUDIT_KEY = "_requirement_evidence_audit"
PATCH_HISTORY_KEY = "_patch_history"

DEFAULT_REQUIREMENTS = {
    "objective": {
        "description": "Objetivo do assistente",
        "stage": "objective",
        "blocking": True,
    },
    "audience": {
        "description": "Públicos ou grupos atendidos",
        "stage": "audience",
        "blocking": True,
    },
    "context": {
        "description": "Contexto e preferências e limites para interpretar mensagens",
        "stage": "context",
        "blocking": True,
    },
    "tone": {
        "description": "Tom e estilo de comunicação nas respostas",
        "stage": "tone",
        "blocking": True,
    },
    "goals": {
        "description": "Objetivos operacionais do assistente",
        "stage": "goals",
        "blocking": True,
    },
    "knowledge": {
        "description": "Conhecimento autorizado necessário",
        "stage": "knowledge",
        "blocking": True,
    },
    "allowed_actions": {
        "description": "Ações ou respostas permitidas",
        "stage": "allowed_actions",
        "blocking": True,
    },
    "approval_boundaries": {
        "description": "O que precisa de aprovação humana",
        "stage": "approval_boundaries",
        "blocking": True,
    },
    "forbidden_actions": {
        "description": "O que o assistente nunca pode fazer",
        "stage": "forbidden_actions",
        "blocking": True,
    },
    "escalation": {
        "description": "Quando chamar ou sinalizar um humano",
        "stage": "escalation",
        "blocking": True,
    },
}

LEGACY_REQUIREMENT_IDS = {
    "canais ou tipos de mensagem atendidos": "knowledge",
    "canal atendido": "knowledge",
    "canais atendidos": "knowledge",
    "contexto": "context",
    "contexto e preferências e limites para interpretar mensagens": "context",
    "contexto, preferências e limites para interpretar mensagens": "context",
    "preferências e limites para interpretar mensagens": "context",
    "preferências e limites que o assistente deve considerar ao interpretar e responder mensagens": "context",
    "preferencias e limites que o assistente deve considerar ao interpretar e responder mensagens": "context",
    "fontes de contexto que a lia pode consultar no mvp": "context",
    "tom e estilo de comunicação nas respostas": "tone",
    "tom e estilo de comunicacao nas respostas": "tone",
    "regras específicas por grupo de contato": "group_rules",
    "regras especificas por grupo de contato": "group_rules",
    "comportamento esperado da lia ao receber mensagens durante a indisponibilidade do usuário": "default_unavailable_behavior",
    "comportamento esperado da lia ao receber mensagens durante a indisponibilidade do usuario": "default_unavailable_behavior",
    "comportamento padrão para mensagens não urgentes e sem regra específica durante a indisponibilidade": "default_no_rule_behavior",
    "comportamento padrao para mensagens nao urgentes e sem regra especifica durante a indisponibilidade": "default_no_rule_behavior",
}

DYNAMIC_REQUIREMENT_PREFIX = "ai:"

QUESTIONS = {
    "objective": "O que voce quer que esse assistente faca?",
    "audience": "Quem esse assistente deve atender?",
    "context": "Qual problema ou contexto ele precisa entender?",
    "tone": "Como ele deve se comunicar?",
    "goals": "Quais resultados ele deve buscar?",
    "knowledge": "Que conhecimento ele precisa ter para funcionar bem?",
    "allowed_actions": "O que ele pode fazer sozinho, em modo seguro?",
    "approval_boundaries": "O que precisa de aprovacao humana?",
    "forbidden_actions": "O que ele nunca pode fazer?",
    "escalation": "Quando ele deve chamar um humano?",
    "autonomy": "Qual nivel de autonomia inicial voce espera? Nesta V0 sera limitado a observe.",
    "review": "Revise o resumo e envie uma correcao se algo estiver errado.",
}

ESSENTIAL_FIELDS = [
    "objective",
    "audience",
    "context",
    "tone",
    "goals",
    "knowledge",
    "allowed_actions",
    "approval_boundaries",
    "forbidden_actions",
    "escalation",
]

ALLOWED_PATCH_PATHS = {
    "purpose",
    "domain",
    "audiences",
    "tone",
    "goals",
    "knowledge_requirements",
    "rules",
    "constraints",
    "actions.allowed",
    "actions.requires_approval",
    "actions.forbidden",
    "escalation",
    "success_criteria",
}

SPEC_ANSWER_KEYS = {
    "purpose": "objective",
    "domain": "ai_domain",
    "audiences": "audience",
    "tone": "tone",
    "goals": "goals",
    "knowledge_requirements": "knowledge",
    "rules": "ai_rules",
    "constraints": "ai_constraints",
    "actions.allowed": "allowed_actions",
    "actions.requires_approval": "approval_boundaries",
    "actions.forbidden": "forbidden_actions",
    "escalation": "escalation",
    "success_criteria": "ai_success_criteria",
}


class ConfigurationInterviewer(Protocol):
    def next_question(self, row: ConfigurationSessionRow) -> Question | None:
        ...

    def record_answer(self, row: ConfigurationSessionRow, text: str) -> InterviewResult:
        ...


class DeterministicConfigurationInterviewer:
    def next_question(self, row: ConfigurationSessionRow) -> Question | None:
        for stage in STAGES:
            if stage == "review":
                return Question(stage=stage, text=QUESTIONS[stage])
            if stage not in row.answers:
                return Question(stage=stage, text=QUESTIONS[stage])
        return None

    def record_answer(self, row: ConfigurationSessionRow, text: str) -> InterviewResult:
        stage = row.current_stage
        answers = dict(row.answers)
        answers[stage] = text.strip()
        next_stage = _next_unanswered_stage(answers)
        row.answers = answers
        row.current_stage = next_stage
        row.status = (
            ConfigurationSessionStatus.READY_FOR_REVIEW.value
            if next_stage == "review"
            else ConfigurationSessionStatus.ACTIVE.value
        )
        row.updated_at = now_utc()
        return InterviewResult(next_stage=next_stage, status=ConfigurationSessionStatus(row.status))


def _next_unanswered_stage(answers: dict[str, str]) -> str:
    for stage in STAGES:
        if stage == "review" or stage not in answers:
            return stage
    return "review"


def _split_answer(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.replace("\n", ",").replace(";", ",")
    return [item.strip(" .") for item in raw.split(",") if item.strip(" .")]


def _canonical_text(value: str) -> str:
    return " ".join(value.strip(" .").lower().split())


def _requirement_id_for_description(description: str) -> str:
    canonical = _canonical_text(description)
    for requirement_id, cfg in DEFAULT_REQUIREMENTS.items():
        if canonical == _canonical_text(cfg["description"]):
            return requirement_id
    if canonical in LEGACY_REQUIREMENT_IDS:
        return LEGACY_REQUIREMENT_IDS[canonical]
    return f"{DYNAMIC_REQUIREMENT_PREFIX}{stable_hash(canonical)[:16]}"


def _has_any(answers: dict[str, Any], key: str, terms: list[str]) -> bool:
    text = str(answers.get(key, "")).lower()
    return any(term.lower() in text for term in terms)


def _answer_has_significant_value(answers: dict[str, Any], path: str) -> bool:
    answer_key = SPEC_ANSWER_KEYS.get(path)
    if not answer_key:
        return False
    value = answers.get(answer_key)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return value is not None


def _dynamic_evidence_for_requirement(answers: dict[str, Any], requirement_id: str) -> str | None:
    evidence_by_requirement = answers.get(REQUIREMENT_EVIDENCE_KEY) or {}
    refs = evidence_by_requirement.get(requirement_id) or []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        if ref.get("coverage") != RequirementEvidenceCoverage.COMPLETE.value:
            continue
        paths = ref.get("evidence_paths") or []
        if paths and all(_answer_has_significant_value(answers, path) for path in paths):
            return f"{ref.get('type', 'requirement_evidence')}:{ref.get('turn_ref')}:{ref.get('patch_refs')}"
    return None


def _partial_evidence_for_requirement(answers: dict[str, Any], requirement_id: str) -> list[dict[str, Any]]:
    evidence_by_requirement = answers.get(REQUIREMENT_EVIDENCE_KEY) or {}
    refs = evidence_by_requirement.get(requirement_id) or []
    partial = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        if ref.get("coverage") == RequirementEvidenceCoverage.PARTIAL.value:
            paths = ref.get("evidence_paths") or []
            if paths and all(_answer_has_significant_value(answers, path) for path in paths):
                partial.append(ref)
    return partial


def _evidence_for_requirement(answers: dict[str, Any], requirement_id: str) -> str | None:
    checks = {
        "objective": lambda: "objective" if str(answers.get("objective", "")).strip() else None,
        "audience": lambda: "audience" if str(answers.get("audience", "")).strip() else None,
        "context": lambda: (
            "context"
            if str(answers.get("context", "")).strip()
            else (
                "knowledge/rules/constraints"
                if any(str(answers.get(key, "")).strip() for key in ("knowledge", "ai_rules", "ai_constraints"))
                else None
            )
        ),
        "tone": lambda: "tone" if str(answers.get("tone", "")).strip() else None,
        "goals": lambda: "goals/domain_defaults"
        if str(answers.get("goals", "")).strip()
        or _default_goals(str(answers.get("ai_domain") or _infer_domain(answers.get("objective", ""), answers.get("context", ""))))
        else None,
        "knowledge": lambda: "knowledge" if str(answers.get("knowledge", "")).strip() else None,
        "allowed_actions": lambda: "allowed_actions" if str(answers.get("allowed_actions", "")).strip() else None,
        "approval_boundaries": lambda: "approval_boundaries" if str(answers.get("approval_boundaries", "")).strip() else None,
        "forbidden_actions": lambda: "forbidden_actions" if str(answers.get("forbidden_actions", "")).strip() else None,
        "escalation": lambda: "escalation" if str(answers.get("escalation", "")).strip() else None,
        "group_rules": lambda: "ai_rules"
        if _has_any(answers, "ai_rules", ["Para familiares", "Para recrutadores", "Para números desconhecidos"])
        else None,
        "default_unavailable_behavior": lambda: "ai_rules"
        if _has_any(answers, "ai_rules", ["confirmar o recebimento", "previsão genérica de retorno"])
        else None,
        "default_no_rule_behavior": lambda: "ai_rules"
        if _has_any(answers, "ai_rules", ["não houver regra aplicável", "reconhecer a lacuna"])
        else None,
    }
    checker = checks.get(requirement_id)
    if checker:
        return checker()
    if requirement_id.startswith(DYNAMIC_REQUIREMENT_PREFIX):
        return _dynamic_evidence_for_requirement(answers, requirement_id)
    return None


def _requirement_description(requirement_id: str, fallback: str | None = None) -> str:
    if requirement_id in DEFAULT_REQUIREMENTS:
        return DEFAULT_REQUIREMENTS[requirement_id]["description"]
    descriptions = {
        "group_rules": "Regras específicas por grupo de contato",
        "default_unavailable_behavior": "Comportamento esperado da Lia ao receber mensagens durante a indisponibilidade do usuário",
        "default_no_rule_behavior": "Comportamento padrão para mensagens não urgentes e sem regra específica durante a indisponibilidade",
    }
    return descriptions.get(requirement_id, fallback or requirement_id)


def _default_requirement_state() -> dict[str, dict[str, Any]]:
    return {
        requirement_id: {
            "id": requirement_id,
            "description": cfg["description"],
            "stage": cfg["stage"],
            "blocking": cfg["blocking"],
            "source": "schema_default",
            "status": "missing",
            "evidence": None,
        }
        for requirement_id, cfg in DEFAULT_REQUIREMENTS.items()
    }


def _stage_from_requirements(requirements: dict[str, dict[str, Any]]) -> str:
    for stage in STAGES:
        if stage == "review":
            return stage
        if any(req.get("stage") == stage and req.get("blocking") and req.get("status") != "resolved" for req in requirements.values()):
            return stage
    return "review"


def reconcile_configuration_answers(answers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    reconciled = dict(answers)
    before_missing = _split_answer(reconciled.get("_ai_missing_information", ""))
    existing = dict(reconciled.get(CANONICAL_REQUIREMENTS_KEY) or {})
    requirements = _default_requirement_state()

    for requirement_id, data in existing.items():
        if isinstance(data, dict):
            merged = {
                "id": requirement_id,
                "description": data.get("description") or _requirement_description(requirement_id),
                "stage": data.get("stage") or DEFAULT_REQUIREMENTS.get(requirement_id, {}).get("stage", "context"),
                "blocking": bool(data.get("blocking", True)),
                "source": data.get("source", "ai"),
                "status": "missing",
                "evidence": None,
            }
            if data.get("partial_evidence"):
                merged["partial_evidence"] = data.get("partial_evidence")
            requirements[requirement_id] = merged

    for description in before_missing:
        requirement_id = _requirement_id_for_description(description)
        requirements.setdefault(
            requirement_id,
            {
                "id": requirement_id,
                "description": _requirement_description(requirement_id, description),
                "stage": "context",
                "blocking": True,
                "source": "legacy_ai_missing",
                "status": "missing",
                "evidence": None,
            },
        )

    for requirement_id, requirement in requirements.items():
        evidence = _evidence_for_requirement(reconciled, requirement_id)
        if evidence:
            requirement["status"] = "resolved"
            requirement["evidence"] = evidence
            requirement.pop("partial_evidence", None)
        else:
            requirement["status"] = "missing"
            requirement["evidence"] = None
            partial = _partial_evidence_for_requirement(reconciled, requirement_id)
            if partial:
                requirement["partial_evidence"] = partial
            else:
                requirement.pop("partial_evidence", None)

    requirements = {
        requirement_id: requirements[requirement_id]
        for requirement_id in sorted(requirements)
    }
    missing = [
        req["description"]
        for req in requirements.values()
        if req.get("blocking") and req.get("status") != "resolved"
    ]
    resolved = [
        req["description"]
        for req in requirements.values()
        if req.get("status") == "resolved"
    ]
    reconciled[CANONICAL_REQUIREMENTS_KEY] = requirements
    reconciled["_ai_missing_information"] = ", ".join(missing)
    reconciled["_ai_resolved_information"] = ", ".join(resolved)
    reconciled["_ai_needs_clarification"] = bool(missing)
    return reconciled, {"missing": missing, "resolved": resolved, "requirements": requirements}


def reconcile_configuration_session(row: ConfigurationSessionRow) -> dict[str, Any]:
    before = dict(row.answers)
    answers, summary = reconcile_configuration_answers(before)
    row.answers = answers
    row.current_stage = _stage_from_requirements(summary["requirements"])
    row.status = (
        ConfigurationSessionStatus.READY_FOR_REVIEW.value
        if row.current_stage == "review"
        else ConfigurationSessionStatus.ACTIVE.value
    )
    row.updated_at = now_utc()
    return {
        "before_missing": _split_answer(before.get("_ai_missing_information", "")),
        "after_missing": summary["missing"],
        "resolved": summary["resolved"],
        "current_stage": row.current_stage,
    }


def reconcile_configuration_session_state(
    session: Session, session_id: str, dry_run: bool = True
) -> dict[str, Any]:
    row = get_configuration_session(session, session_id)
    original_answers = copy.deepcopy(row.answers)
    original_stage = row.current_stage
    original_status = row.status
    summary = reconcile_configuration_session(row)
    changed = (
        row.answers != original_answers
        or row.current_stage != original_stage
        or row.status != original_status
    )
    result = {
        "session_id": row.id,
        "dry_run": dry_run,
        "changed": changed,
        "before_stage": original_stage,
        "after_stage": row.current_stage,
        "before_missing": summary["before_missing"],
        "after_missing": summary["after_missing"],
        "resolved": summary["resolved"],
    }
    if dry_run:
        row.answers = original_answers
        row.current_stage = original_stage
        row.status = original_status
        return result
    if changed:
        session.flush()
        audit(
            session,
            None,
            "configuration_session_reconciled",
            {
                "session_id": row.id,
                "before_missing_count": len(result["before_missing"]),
                "after_missing_count": len(result["after_missing"]),
                "before_stage": original_stage,
                "after_stage": row.current_stage,
            },
            origin="agent_builder",
        )
    return result


def recover_requirement_evidence_link(
    session: Session,
    session_id: str,
    requirement_id: str,
    evidence_path: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    row = get_configuration_session(session, session_id)
    original_answers = copy.deepcopy(row.answers)
    original_stage = row.current_stage
    original_status = row.status
    answers = dict(row.answers)
    requirements = answers.get(CANONICAL_REQUIREMENTS_KEY) or {}
    requirement = requirements.get(requirement_id)
    patch_history = answers.get(PATCH_HISTORY_KEY) or []
    matching_patch = None
    for turn in reversed(patch_history):
        for patch in turn.get("accepted", []):
            if patch.get("path") == evidence_path:
                matching_patch = {
                    "turn_ref": turn.get("turn_ref"),
                    "patch_ref": patch.get("patch_ref"),
                    "path": patch.get("path"),
                }
                break
        if matching_patch:
            break
    if matching_patch is None:
        legacy_audit = session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.event_type == "ai_interviewer_success")
            .order_by(AuditEventRow.created_at.desc())
        ).all()
        for event in legacy_audit:
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("session_id") != session_id or int(payload.get("applied_patches") or 0) <= 0:
                continue
            event_ref = stable_hash(
                {
                    "session_id": session_id,
                    "requirement_id": requirement_id,
                    "evidence_path": evidence_path,
                    "audit_event_id": event.id,
                }
            )[:16]
            matching_patch = {
                "turn_ref": f"legacy-turn:{event_ref}",
                "patch_ref": f"legacy-patch:{event_ref}",
                "path": evidence_path,
            }
            break
    checks = {
        "requirement_exists": isinstance(requirement, dict),
        "requirement_missing": isinstance(requirement, dict) and requirement.get("status") == "missing",
        "requirement_without_evidence": isinstance(requirement, dict) and not requirement.get("evidence"),
        "path_allowed": evidence_path in ALLOWED_PATCH_PATHS,
        "path_has_value": _answer_has_significant_value(answers, evidence_path),
        "accepted_patch_found": matching_patch is not None,
        "stage_context": row.current_stage == "context",
        "needs_clarification": answers.get("_ai_needs_clarification") is True,
    }
    can_recover = all(checks.values())
    before_missing = _split_answer(answers.get("_ai_missing_information", ""))
    if can_recover:
        ref = {
            "type": "accepted_patch",
            "requirement_id": requirement_id,
            "evidence_paths": [evidence_path],
            "turn_ref": matching_patch["turn_ref"],
            "patch_refs": [matching_patch["patch_ref"]],
            "coverage": RequirementEvidenceCoverage.COMPLETE.value,
            "origin": "admin_recovery",
        }
        _persist_requirement_evidence(row, [ref])
        summary = reconcile_configuration_session(row)
    else:
        summary = {
            "before_missing": before_missing,
            "after_missing": before_missing,
            "resolved": [],
            "current_stage": row.current_stage,
        }
    changed = row.answers != original_answers or row.current_stage != original_stage or row.status != original_status
    result = {
        "session_id": row.id,
        "dry_run": dry_run,
        "changed": changed,
        "checks": checks,
        "before_stage": original_stage,
        "after_stage": row.current_stage,
        "before_missing": before_missing,
        "after_missing": summary["after_missing"],
        "requirement_id": requirement_id,
        "evidence_path": evidence_path,
        "matching_patch": matching_patch,
    }
    if dry_run:
        row.answers = original_answers
        row.current_stage = original_stage
        row.status = original_status
        return result
    if can_recover and changed:
        audit(
            session,
            None,
            "configuration_requirement_evidence_recovered",
            {
                "session_id": row.id,
                "requirement_id": requirement_id,
                "evidence_path": evidence_path,
                "turn_ref": matching_patch["turn_ref"],
                "patch_ref": matching_patch["patch_ref"],
                "coverage": RequirementEvidenceCoverage.COMPLETE.value,
            },
            origin="agent_builder",
        )
    return result


def _infer_domain(objective: str, context: str) -> str:
    text = f"{objective} {context}".lower()
    if any(word in text for word in ["pizzaria", "pizza", "pedido", "delivery", "retirada"]):
        return "food_service"
    if any(word in text for word in ["mensagens", "atencao", "atenção", "prioridade", "contatos"]):
        return "personal_attention"
    if any(word in text for word in ["suporte", "ticket", "tecnico", "técnico"]):
        return "technical_support"
    if any(word in text for word in ["clinica", "clínica", "recepcao", "recepção", "consulta"]):
        return "clinic_reception"
    return "general"


def _default_goals(domain: str) -> list[str]:
    defaults = {
        "personal_attention": ["triage messages", "identify priority", "escalate important contacts"],
        "food_service": ["receive orders", "collect order details", "clarify delivery/pickup", "escalate exceptions"],
    }
    return defaults.get(domain, [])


def _domain_missing_information(domain: str, knowledge_answer: str) -> list[str]:
    text = knowledge_answer.lower()
    missing = []
    if domain == "food_service":
        for key, terms in {
            "menu": ["cardapio", "cardápio", "menu"],
            "prices": ["preco", "preço", "valor"],
            "delivery area": ["area", "área", "bairro", "entrega"],
            "business hours": ["horario", "horário", "funcionamento"],
        }.items():
            if not any(term in text for term in terms):
                missing.append(key)
    return missing


def _completeness(answers: dict[str, str], missing: list[str]) -> int:
    answered = sum(1 for field in ESSENTIAL_FIELDS if answers.get(field, "").strip())
    base = int(answered / len(ESSENTIAL_FIELDS) * 100)
    return max(0, min(100, base - min(len(missing) * 5, 30)))


def create_configuration_session(session: Session) -> ConfigurationSessionRow:
    stamp = now_utc()
    row = ConfigurationSessionRow(
        id=new_id(),
        status=ConfigurationSessionStatus.ACTIVE.value,
        blueprint_id=None,
        current_stage="objective",
        answers={},
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    audit(session, None, "configuration_session_created", {"session_id": row.id}, origin="agent_builder")
    return row


def get_configuration_session(session: Session, session_id: str) -> ConfigurationSessionRow:
    row = session.get(ConfigurationSessionRow, session_id)
    if row is None:
        raise KeyError(session_id)
    return row


def get_next_question(session: Session, session_id: str) -> Question | None:
    row = get_configuration_session(session, session_id)
    return DeterministicConfigurationInterviewer().next_question(row)


def _select_ai_interviewer(provider: str | None = None):
    selected = provider or settings.agent_builder_interviewer_provider
    if selected == "openai":
        return OpenAIConfigurationInterviewer()
    if selected == "deterministic":
        return None
    raise ValueError("unknown interviewer provider")


def _compact_context(row: ConfigurationSessionRow, user_text: str) -> dict[str, Any]:
    spec = build_blueprint_spec_from_session(row)
    requirements = row.answers.get(CANONICAL_REQUIREMENTS_KEY, {})
    return {
        "session_id": row.id,
        "prompt_version": AI_INTERVIEWER_PROMPT_VERSION,
        "current_stage": row.current_stage,
        "objective": row.answers.get("objective"),
        "answers": {key: row.answers.get(key) for key in ESSENTIAL_FIELDS if row.answers.get(key)},
        "current_summary": spec.model_dump(mode="json"),
        "requirements": [
            {
                "id": req.get("id"),
                "description": req.get("description"),
                "status": req.get("status"),
                "stage": req.get("stage"),
                "partial_evidence": [
                    {
                        "evidence_paths": item.get("evidence_paths", []),
                        "coverage": item.get("coverage"),
                    }
                    for item in (req.get("partial_evidence") or [])
                    if isinstance(item, dict)
                ],
            }
            for req in requirements.values()
        ],
        "missing_information": spec.missing_information,
        "allowed_patch_paths": sorted(ALLOWED_PATCH_PATHS),
        "last_user_message": user_text,
    }


def _coerce_patch_value(path: str, value: Any) -> str | list[str]:
    if path in {"purpose", "domain", "tone"}:
        if hasattr(value, "style") and path == "tone":
            return str(value.style or getattr(value, "notes", "") or "")
        if isinstance(value, dict) and path == "tone":
            return str(value.get("style") or value.get("notes") or "")
        return str(value)
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value)]


def apply_blueprint_patch_to_answers(row: ConfigurationSessionRow, patch: BlueprintPatch) -> bool:
    if patch.path not in ALLOWED_PATCH_PATHS:
        return False
    if patch.path.startswith("autonomy") or patch.path in {"status", "published", "version", "id", "created_at"}:
        return False
    answer_key = SPEC_ANSWER_KEYS[patch.path]
    answers = dict(row.answers)
    value = _coerce_patch_value(patch.path, patch.value)
    if isinstance(value, list):
        existing = _split_answer(answers.get(answer_key, ""))
        if patch.operation == BlueprintPatchOperation.APPEND:
            merged = existing + [item for item in value if item not in existing]
        elif patch.operation == BlueprintPatchOperation.REMOVE:
            merged = [item for item in existing if item not in set(value)]
        else:
            merged = value
        answers[answer_key] = ", ".join(merged)
    else:
        if patch.operation == BlueprintPatchOperation.REMOVE:
            answers.pop(answer_key, None)
        else:
            answers[answer_key] = value
    row.answers = answers
    reconcile_summary = reconcile_configuration_session(row)
    row.status = (
        ConfigurationSessionStatus.READY_FOR_REVIEW.value
        if reconcile_summary["current_stage"] == "review"
        else ConfigurationSessionStatus.ACTIVE.value
    )
    row.updated_at = now_utc()
    return True


def _patch_ref(turn_ref: str, index: int, patch: BlueprintPatch) -> str:
    return f"patch:{stable_hash({'turn_ref': turn_ref, 'index': index, 'path': patch.path, 'operation': patch.operation.value})[:16]}"


def _record_patch_history(row: ConfigurationSessionRow, turn_ref: str, accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    answers = dict(row.answers)
    history = list(answers.get(PATCH_HISTORY_KEY, []))
    history.append(
        {
            "turn_ref": turn_ref,
            "accepted": [
                {"patch_ref": item["patch_ref"], "path": item["path"], "operation": item["operation"]}
                for item in accepted
            ],
            "rejected": [
                {"patch_ref": item["patch_ref"], "path": item["path"], "operation": item["operation"]}
                for item in rejected
            ],
        }
    )
    answers[PATCH_HISTORY_KEY] = history
    row.answers = answers


def _validate_requirement_evidence(
    row: ConfigurationSessionRow,
    proposed: list[Any],
    before_requirements: dict[str, dict[str, Any]],
    accepted_patches: list[dict[str, Any]],
    turn_ref: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted_by_path: dict[str, list[dict[str, Any]]] = {}
    for patch in accepted_patches:
        accepted_by_path.setdefault(patch["path"], []).append(patch)
    accepted = []
    rejected = []
    answers = dict(row.answers)
    for item in proposed:
        requirement_id = item.requirement_id
        paths = list(dict.fromkeys(item.evidence_paths))
        coverage = item.coverage.value
        if requirement_id not in before_requirements:
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "unknown_requirement_id"})
            continue
        before_status = before_requirements[requirement_id].get("status")
        if before_status == "resolved":
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "requirement_already_resolved"})
            continue
        if not paths:
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "missing_evidence_paths"})
            continue
        invalid_paths = [path for path in paths if path not in ALLOWED_PATCH_PATHS]
        if invalid_paths:
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "path_not_allowed"})
            continue
        if any(path not in accepted_by_path for path in paths):
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "path_not_accepted_in_turn"})
            continue
        if any(not _answer_has_significant_value(answers, path) for path in paths):
            rejected.append({"requirement_id": requirement_id, "evidence_paths": paths, "reason": "empty_persisted_evidence"})
            continue
        patch_refs = [patch["patch_ref"] for path in paths for patch in accepted_by_path[path]]
        accepted.append(
            {
                "type": "accepted_patch",
                "requirement_id": requirement_id,
                "evidence_paths": paths,
                "turn_ref": turn_ref,
                "patch_refs": patch_refs,
                "coverage": coverage,
                "origin": "provider_requirement_evidence",
            }
        )
    return accepted, rejected


def _persist_requirement_evidence(row: ConfigurationSessionRow, refs: list[dict[str, Any]]) -> None:
    if not refs:
        return
    answers = dict(row.answers)
    evidence = copy.deepcopy(answers.get(REQUIREMENT_EVIDENCE_KEY) or {})
    for ref in refs:
        requirement_id = ref["requirement_id"]
        existing = evidence.setdefault(requirement_id, [])
        comparable = {k: ref[k] for k in ("type", "evidence_paths", "turn_ref", "patch_refs", "coverage", "origin")}
        if not any({k: item.get(k) for k in comparable} == comparable for item in existing if isinstance(item, dict)):
            existing.append(comparable)
    answers[REQUIREMENT_EVIDENCE_KEY] = evidence
    row.answers = answers


def chat_configuration_session(
    session: Session,
    session_id: str,
    text: str,
    provider: str | None = None,
    fallback_to_deterministic: bool = True,
    interviewer: Any | None = None,
) -> dict[str, Any]:
    row = get_configuration_session(session, session_id)
    answers = dict(row.answers)
    turns = list(answers.get("_conversation_turns", []))
    turns.append({"role": "user", "text": text})
    answers["_conversation_turns"] = turns[-8:]
    row.answers = answers
    row.updated_at = now_utc()
    session.flush()
    selected_provider = provider or settings.agent_builder_interviewer_provider
    if selected_provider == "deterministic" and interviewer is None:
        recorded = record_configuration_answer(session, session_id, text)
        question = get_next_question(session, session_id)
        return {
            "provider": "deterministic",
            "assistant_message": question.text if question else "Sessao pronta para revisao.",
            "next_question": question.text if question else "",
            "ready_for_review": recorded.status == ConfigurationSessionStatus.READY_FOR_REVIEW.value,
            "applied_patches": [],
            "rejected_patches": [],
            "fallback_used": False,
        }
    started = time.monotonic()
    try:
        active_interviewer = interviewer or _select_ai_interviewer(selected_provider)
        if active_interviewer is None:
            raise ValueError("deterministic interviewer selected")
        turn = active_interviewer.interview_turn(_compact_context(row, text), text)
        before_requirements = copy.deepcopy((dict(row.answers)).get(CANONICAL_REQUIREMENTS_KEY) or {})
        turn_ref = f"turn:{stable_hash({'session_id': row.id, 'turn_count': len(turns), 'user_text': text})[:16]}"
        applied = []
        rejected = []
        accepted_patch_refs = []
        rejected_patch_refs = []
        for index, patch in enumerate(turn.proposed_updates):
            ref = _patch_ref(turn_ref, index, patch)
            if apply_blueprint_patch_to_answers(row, patch):
                patch_data = patch.model_dump(mode="json")
                applied.append(patch_data)
                accepted_patch_refs.append(
                    {"patch_ref": ref, "path": patch.path, "operation": patch.operation.value}
                )
            else:
                patch_data = patch.model_dump(mode="json")
                rejected.append(patch_data)
                rejected_patch_refs.append(
                    {"patch_ref": ref, "path": patch.path, "operation": patch.operation.value}
                )
        _record_patch_history(row, turn_ref, accepted_patch_refs, rejected_patch_refs)
        accepted_evidence, rejected_evidence = _validate_requirement_evidence(
            row, turn.requirement_evidence, before_requirements, accepted_patch_refs, turn_ref
        )
        _persist_requirement_evidence(row, accepted_evidence)
        answers = dict(row.answers)
        if turn.new_missing_information:
            current = _split_answer(answers.get("_ai_missing_information", ""))
            merged = current + [item for item in turn.new_missing_information if item not in current]
            answers["_ai_missing_information"] = ", ".join(merged)
        if turn.resolved_information:
            previous = _split_answer(answers.get("_ai_resolved_information", ""))
            answers["_ai_resolved_information"] = ", ".join(previous + [item for item in turn.resolved_information if item not in previous])
        if turn.needs_clarification and not answers.get("_ai_needs_clarification"):
            answers["_ai_needs_clarification"] = True
        turns = list(answers.get("_conversation_turns", []))
        turns.append({"role": "assistant", "text": turn.assistant_message})
        answers["_conversation_turns"] = turns[-8:]
        row.answers = answers
        reconcile_summary = reconcile_configuration_session(row)
        if turn.ready_for_review and not reconcile_summary["after_missing"]:
            row.status = ConfigurationSessionStatus.READY_FOR_REVIEW.value
        row.updated_at = now_utc()
        session.flush()
        latency_ms = int((time.monotonic() - started) * 1000)
        audit(
            session,
            None,
            "ai_interviewer_success",
            {
                "session_id": row.id,
                "provider": getattr(active_interviewer, "provider_name", selected_provider),
                "model": getattr(active_interviewer, "model", "unknown"),
                "latency_ms": latency_ms,
                "schema_validation": "ok",
                "applied_patches": len(applied),
                "rejected_patches": len(rejected),
                "requirement_evidence_proposed": len(turn.requirement_evidence),
                "requirement_evidence_accepted": len(accepted_evidence),
                "requirement_evidence_rejected": len(rejected_evidence),
                "requirement_evidence_rejection_reasons": sorted(
                    {item["reason"] for item in rejected_evidence}
                ),
                "requirement_evidence_ids": [
                    item["requirement_id"] for item in accepted_evidence
                ],
                "requirement_evidence_paths": sorted(
                    {path for item in accepted_evidence for path in item["evidence_paths"]}
                ),
            },
            origin="agent_builder",
        )
        return {
            "provider": getattr(active_interviewer, "provider_name", selected_provider),
            "model": getattr(active_interviewer, "model", "unknown"),
            "assistant_message": turn.assistant_message,
            "next_question": turn.next_question,
            "ready_for_review": turn.ready_for_review,
            "needs_clarification": turn.needs_clarification,
            "confidence": turn.confidence,
            "reason_codes": turn.reason_codes,
            "applied_patches": applied,
            "rejected_patches": rejected,
            "accepted_requirement_evidence": accepted_evidence,
            "rejected_requirement_evidence": rejected_evidence,
            "fallback_used": False,
        }
    except (AIInterviewerError, ValueError) as exc:
        audit(
            session,
            None,
            "ai_interviewer_failure",
            {
                "session_id": row.id,
                "provider": selected_provider,
                "model": settings.agent_builder_openai_model if selected_provider == "openai" else "deterministic",
                "error_code": getattr(exc, "code", "interviewer_error"),
                "retryable": bool(getattr(exc, "retryable", False)),
            },
            origin="agent_builder",
        )
        if not fallback_to_deterministic:
            raise
        recorded = record_configuration_answer(session, session_id, text)
        question = get_next_question(session, session_id)
        return {
            "provider": selected_provider,
            "assistant_message": question.text if question else "Sessao pronta para revisao.",
            "next_question": question.text if question else "",
            "ready_for_review": recorded.status == ConfigurationSessionStatus.READY_FOR_REVIEW.value,
            "applied_patches": [],
            "rejected_patches": [],
            "fallback_used": True,
            "error_code": getattr(exc, "code", "interviewer_error"),
        }


def record_configuration_answer(session: Session, session_id: str, text: str) -> ConfigurationSessionRow:
    row = get_configuration_session(session, session_id)
    previous_stage = row.current_stage
    DeterministicConfigurationInterviewer().record_answer(row, text)
    session.flush()
    audit(
        session,
        None,
        "configuration_answer_recorded",
        {"session_id": row.id, "stage": previous_stage},
        origin="agent_builder",
    )
    return row


def correct_configuration_answer(session: Session, session_id: str, stage: str, text: str) -> ConfigurationSessionRow:
    if stage not in STAGES or stage == "review":
        raise ValueError("unknown configurable stage")
    row = get_configuration_session(session, session_id)
    answers = dict(row.answers)
    answers[stage] = text.strip()
    row.answers = answers
    row.current_stage = _next_unanswered_stage(answers)
    row.status = (
        ConfigurationSessionStatus.READY_FOR_REVIEW.value
        if row.current_stage == "review"
        else ConfigurationSessionStatus.ACTIVE.value
    )
    row.updated_at = now_utc()
    session.flush()
    audit(session, None, "configuration_answer_recorded", {"session_id": row.id, "stage": stage, "corrected": True}, origin="agent_builder")
    return row


def build_blueprint_spec_from_session(row: ConfigurationSessionRow) -> AgentBlueprintSpec:
    answers, reconciliation = reconcile_configuration_answers(row.answers)
    objective = answers.get("objective", "")
    context = answers.get("context", "")
    domain = answers.get("ai_domain") or _infer_domain(objective, context)
    explicit_goals = _split_answer(answers.get("goals"))
    goals = explicit_goals or _default_goals(domain)
    knowledge = _split_answer(answers.get("knowledge"))
    canonical_missing = set(reconciliation["missing"])
    missing = sorted(set(_domain_missing_information(domain, answers.get("knowledge", ""))) | canonical_missing)
    blocking = [QUESTIONS[field] for field in ESSENTIAL_FIELDS if not answers.get(field, "").strip()]
    blocking.extend([f"Informe {item}." for item in missing])
    name = "Assistente configurado"
    if domain == "food_service":
        name = "Atendente de pizzaria"
    elif domain == "personal_attention":
        name = "Assistente pessoal de atencao"
    spec = AgentBlueprintSpec(
        identity={"name": name, "description": objective},
        purpose=objective,
        domain=domain,
        audiences=_split_answer(answers.get("audience")) or ["customers" if domain == "food_service" else "users"],
        tone={"style": answers.get("tone", ""), "notes": ""},
        goals=goals,
        knowledge_requirements=knowledge + [item for item in missing if item not in knowledge],
        known_information={"context": context} if context else {},
        missing_information=missing,
        assumptions=[],
        rules=_split_answer(answers.get("ai_rules")),
        constraints=_split_answer(answers.get("ai_constraints")),
        actions={
            "allowed": _split_answer(answers.get("allowed_actions")),
            "requires_approval": _split_answer(answers.get("approval_boundaries")),
            "forbidden": _split_answer(answers.get("forbidden_actions")),
        },
        escalation=_split_answer(answers.get("escalation")),
        autonomy={"level": AutonomyLevel.OBSERVE},
        channels=[],
        success_criteria=_split_answer(answers.get("ai_success_criteria")),
        configuration_completeness=_completeness(answers, missing),
        blocking_questions=blocking,
    )
    return spec


def render_review(row: ConfigurationSessionRow) -> str:
    spec = build_blueprint_spec_from_session(row)
    return "\n".join(
        [
            f"Nome: {spec.identity.name}",
            f"Objetivo: {spec.purpose or 'nao informado'}",
            f"Dominio: {spec.domain}",
            f"Publico: {', '.join(spec.audiences) or 'nao informado'}",
            f"Pode fazer sozinho: {', '.join(spec.actions.allowed) or 'nao informado'}",
            f"Precisa de aprovacao: {', '.join(spec.actions.requires_approval) or 'nao informado'}",
            f"Nunca pode: {', '.join(spec.actions.forbidden) or 'nao informado'}",
            f"Informacoes faltantes: {', '.join(spec.missing_information) or 'nenhuma registrada'}",
            f"Autonomia: {spec.autonomy.level.value}",
            f"Completeness: {spec.configuration_completeness}",
        ]
    )


def build_blueprint_from_session(
    session: Session, session_id: str, reason: str = "configuration build"
) -> AgentBlueprintVersionRow:
    row = get_configuration_session(session, session_id)
    spec = build_blueprint_spec_from_session(row)
    stamp = now_utc()
    if row.blueprint_id:
        blueprint = session.get(AgentBlueprintRow, row.blueprint_id)
        if blueprint is None:
            raise KeyError(row.blueprint_id)
        blueprint.name = spec.identity.name
        blueprint.updated_at = stamp
    else:
        blueprint = AgentBlueprintRow(
            id=new_id(),
            name=spec.identity.name,
            status=BlueprintStatus.DRAFT.value,
            current_version_id=None,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(blueprint)
        session.flush()
        row.blueprint_id = blueprint.id
    latest = session.scalars(
        select(AgentBlueprintVersionRow)
        .where(AgentBlueprintVersionRow.blueprint_id == blueprint.id)
        .order_by(AgentBlueprintVersionRow.version.desc())
        .limit(1)
    ).first()
    version_number = (latest.version if latest else 0) + 1
    version = AgentBlueprintVersionRow(
        id=new_id(),
        blueprint_id=blueprint.id,
        version=version_number,
        spec=spec.model_dump(mode="json"),
        checksum=stable_hash(spec.model_dump(mode="json")),
        created_at=stamp,
        created_by="agent_builder",
        change_reason=reason,
        is_immutable=True,
    )
    session.add(version)
    session.flush()
    blueprint.current_version_id = version.id
    row.status = ConfigurationSessionStatus.COMPLETED.value
    row.updated_at = stamp
    audit(session, None, "blueprint_draft_created", {"blueprint_id": blueprint.id, "session_id": row.id}, origin="agent_builder")
    audit(
        session,
        None,
        "blueprint_version_created",
        {"blueprint_id": blueprint.id, "version": version.version},
        origin="agent_builder",
    )
    audit(session, None, "configuration_completed", {"session_id": row.id, "blueprint_id": blueprint.id}, origin="agent_builder")
    return version


def get_agent_blueprint(session: Session, blueprint_id: str) -> dict:
    blueprint = session.get(AgentBlueprintRow, blueprint_id)
    if blueprint is None:
        raise KeyError(blueprint_id)
    versions = session.scalars(
        select(AgentBlueprintVersionRow)
        .where(AgentBlueprintVersionRow.blueprint_id == blueprint.id)
        .order_by(AgentBlueprintVersionRow.version)
    ).all()
    return {
        "id": blueprint.id,
        "name": blueprint.name,
        "status": blueprint.status,
        "current_version_id": blueprint.current_version_id,
        "versions": [
            {
                "id": version.id,
                "version": version.version,
                "spec": version.spec,
                "checksum": version.checksum,
                "created_at": version.created_at.isoformat(),
                "created_by": version.created_by,
                "change_reason": version.change_reason,
            }
            for version in versions
        ],
    }
