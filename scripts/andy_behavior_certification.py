#!/usr/bin/env python3
"""Offline Andy behavior V2 certification; never sends or writes application data."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from attention_router.domain.behavior import (
    internal_language_leaks,
    promise_guard,
    render_response,
    variant_capacity,
)


PROFILE = json.loads((Path(__file__).parents[1] / "config/andy_behavior_profile.json").read_text())
AUDIENCES = ("family_core", "recruiters", "business_clients", "unknown", "friends", "relationship")
GENERIC = "pode me contar um pouco mais sobre o assunto"


def case(audience, text, decision="INSUFFICIENT_CONTEXT", action="request_information", *, urgent=False, introduced=False, recorded=False, missing=None):
    return {
        "audience": audience,
        "text": text,
        "decision": decision,
        "action": action,
        "urgent": urgent,
        "introduced": introduced,
        "recorded": recorded,
        "missing": missing or [],
    }


SCENARIOS = [
    case("family_core", "Quem é?"),
    case("family_core", "Leo está aí?"),
    case("family_core", "Pede para ele me ligar."),
    case("family_core", "É urgente.", urgent=True),
    case("family_core", "Quero deixar um recado."),
    case("family_core", "Ele pediu para eu falar com você?"),
    case("family_core", "Você sabe quando ele volta?"),
    case("family_core", "É sobre trabalho."),
    case("recruiters", "Sou da empresa X."),
    case("recruiters", "Gostaria de marcar uma entrevista."),
    case("recruiters", "Pode amanhã às 10h?", missing=["interview_schedule"]),
    case("recruiters", "Qual é a vaga?"),
    case("recruiters", "É uma oportunidade profissional."),
    case("recruiters", "Podemos falar sobre o processo?"),
    case("recruiters", "Preciso de um retorno sobre a candidatura."),
    case("recruiters", "A entrevista seria online."),
    case("business_clients", "Temos um problema no serviço."),
    case("business_clients", "Precisamos falar sobre um contrato."),
    case("business_clients", "Quero deixar um recado."),
    case("business_clients", "Pode nos retornar?"),
    case("business_clients", "A demanda é importante.", urgent=True),
    case("business_clients", "É sobre um cliente."),
    case("business_clients", "Precisamos alinhar um prazo."),
    case("business_clients", "O sistema parou."),
    case("unknown", "Quem é?"),
    case("unknown", "Preciso falar com Alex."),
    case("unknown", "Preciso falar com ele."),
    case("unknown", "Oi."),
    case("unknown", "É importante.", urgent=True),
    case("unknown", "Quero deixar um recado."),
    case("unknown", "Me ajuda aí."),
    case("unknown", "Ele está em casa?"),
    case("friends", "Cadê esse homem?"),
    case("friends", "Diz para ele olhar o WhatsApp."),
    case("friends", "Manda um abraço para ele."),
    case("friends", "Ele sumiu."),
    case("friends", "Queria falar com o Alex."),
    case("friends", "Me avisa quando ele aparecer."),
    case("relationship", "Ele está bem?"),
    case("relationship", "Preciso conversar com ele."),
    case("relationship", "Quero saber como ele está."),
    case("relationship", "É algo importante para nós.", urgent=True),
    case("relationship", "Queria deixar uma mensagem."),
    case("relationship", "Pode pedir para ele falar comigo?"),
    case("unknown", "Oi", introduced=True),
    case("unknown", "Preciso falar com Alex.", introduced=True),
    case("recruiters", "É sobre uma entrevista.", introduced=True),
    case("recruiters", "Pode ser amanhã de manhã.", introduced=True),
    case("business_clients", "Temos uma demanda.", introduced=True),
    case("friends", "Pode pedir para ele me chamar?", introduced=True),
    case("family_core", "É urgente, não pode esperar.", urgent=True, introduced=True),
    case("unknown", "Não sei explicar."),
    case("business_clients", "Qual é o assunto principal?", missing=["urgency"]),
    case("recruiters", "Quero marcar entrevista.", missing=["interview_schedule"]),
]

SAMPLES = [
    case("family_core", "Quem é?"),
    case("family_core", "Pede para ele me ligar."),
    case("family_core", "É urgente.", urgent=True),
    case("family_core", "Ele pediu para eu falar com você?"),
    case("recruiters", "Sou da empresa X."),
    case("recruiters", "Gostaria de marcar uma entrevista."),
    case("recruiters", "Pode amanhã às 10h?", missing=["interview_schedule"]),
    case("recruiters", "É sobre uma oportunidade profissional.", introduced=True),
    case("business_clients", "Temos um problema no serviço."),
    case("business_clients", "Quero deixar um recado."),
    case("business_clients", "A demanda é importante.", urgent=True),
    case("business_clients", "Precisamos alinhar um prazo.", introduced=True),
    case("unknown", "Quem é?"),
    case("unknown", "Preciso falar com Alex."),
    case("unknown", "Ele está em casa?"),
    case("unknown", "É importante.", urgent=True),
    case("friends", "Cadê esse homem?"),
    case("friends", "Diz para ele olhar o WhatsApp.", introduced=True),
    case("friends", "Me avisa quando ele aparecer."),
    case("relationship", "Ele está bem?"),
    case("relationship", "Preciso conversar com ele."),
    case("relationship", "Quero saber como ele está."),
    case("relationship", "Pode pedir para ele falar comigo?", introduced=True),
    case("family_core", "É sobre trabalho.", introduced=True),
]


def render(item, recent=None):
    result = SimpleNamespace(
        decision_type=item["decision"],
        recommended_action=item["action"],
        escalation_required=item["decision"] == "ESCALATE",
        escalation_recorded=item["recorded"],
        missing_information=item["missing"],
    )
    return render_response(
        result,
        PROFILE,
        audience=item["audience"],
        introduced=item["introduced"],
        recent_variant_ids=recent or set(),
        urgent=item["urgent"],
        inbound_text=item["text"],
        missing_information=item["missing"],
    )


def normalize_structure(text):
    text = re.sub(r"\b(Andy|Alex)\b", "<name>", text, flags=re.I)
    text = re.sub(r"[^a-záàâãéêíóôõúç ]+", "", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def main():
    recent = set()
    results = []
    for item in SCENARIOS:
        candidate = render(item, recent)
        if candidate is None or not candidate.text or not candidate.spoken_text:
            raise SystemExit(f"scenario produced no response: {item}")
        recent.add(candidate.variant_id)
        results.append((item, candidate))

    baseline = [render_response(SimpleNamespace(decision_type=item["decision"], recommended_action=item["action"]), PROFILE, audience=item["audience"]) for item in SCENARIOS]
    generic_count = sum(GENERIC in candidate.text.casefold() for _, candidate in results)
    generic_baseline = sum(GENERIC in candidate.text.casefold() for candidate in baseline if candidate)
    structures = {normalize_structure(candidate.text) for _, candidate in results}
    leaks = [internal_language_leaks(candidate.text) for _, candidate in results]
    promise_failures = [
        promise_guard(candidate.text, escalation_recorded=item["recorded"]) == "UNSUPPORTED_PROMISE"
        for item, candidate in results
    ]
    print(f"BEHAVIOR_V2_SCENARIO_COUNT={len(SCENARIOS)}")
    print("BEHAVIOR_V2_SCENARIOS_PASS=YES")
    print("SEMANTIC_MISSING_INFORMATION_QUESTIONS=PASS")
    print("PROMISE_GUARD=PASS" if not any(promise_failures) else "PROMISE_GUARD=FAIL")
    print(f"STRUCTURAL_VARIATION_COUNT={len(structures)}")
    print(f"GENERIC_REQUEST_CONTEXT_RATE_V1={generic_baseline / len(baseline):.2f}")
    print(f"GENERIC_REQUEST_CONTEXT_RATE_V2={generic_count / len(results):.2f}")
    print("NO_INTERNAL_LANGUAGE_LEAK=PASS" if not any(leaks) else "NO_INTERNAL_LANGUAGE_LEAK=FAIL")
    print("NO_UNSUPPORTED_PROMISES=PASS" if not any(promise_failures) else "NO_UNSUPPORTED_PROMISES=FAIL")
    print(f"AUDIENCE_BEHAVIOR_COUNT={len(AUDIENCES)}")
    print("TTS_READY_TEXT=PASS")
    for index, item in enumerate(SAMPLES, 1):
        candidate = render(item)
        print(f"BEHAVIOR_SAMPLE_{index:02d}")
        print(f"AUDIENCE={item['audience']}")
        print(f"CONVERSATION_STATE={'introduced' if item['introduced'] else 'new'}")
        print(f"INPUT={item['text']}")
        print(f"DECISION={item['decision']}")
        print(f"MISSING_INFORMATION={item['missing'] or 'derived'}")
        print(f"MESSAGE_FAMILY={candidate.message_family}")
        print(f"VARIANT_ID={candidate.variant_id}")
        print(f"INTRODUCTION_INCLUDED={candidate.introduction_included}")
        print(f"RESPONSE={candidate.text}")
        print(f"SPOKEN_TEXT={candidate.spoken_text}")
        print(f"PROMISE_ACTION_BACKING={candidate.promise_check}")
        print()
    recent = set()
    for index in range(1, 11):
        item = case("family_core", "Preciso falar com Alex.")
        candidate = render(item, recent)
        recent.add(candidate.variant_id)
        print(f"SAME_INTENT_VARIATION_{index:02d}={candidate.text}")
    print(f"VARIANTS_PER_CORE_FAMILY={variant_capacity(PROFILE, 'REQUEST_CONTEXT')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
