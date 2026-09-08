from attention_router.domain.models import Interaction, Policy


class DeterministicLanguageAdapter:
    def render_reply(self, interaction: Interaction, policy: Policy) -> str:
        templates = {
            "proximo": "Lia: vou avisar Alex com prioridade alta e aguardar confirmação.",
            "cordial_brincalhao": "Lia: recado recebido. Vou chamar Alex de um jeito leve e confirmar retorno.",
            "profissional": "Lia: obrigada pelo contato. Vou sinalizar a prioridade profissional e pedir retorno.",
            "neutro_seguro": "Lia: mensagem recebida. Vou registrar o contato sem compartilhar detalhes privados.",
        }
        return templates.get(policy.tone, templates["neutro_seguro"])


class MockChannelAdapter:
    def accept_event(self, payload: dict) -> dict:
        return {"accepted": True, "synthetic": True, "payload": payload}


class MockActionAdapter:
    def dispatch(self, action_key: str, interaction_id: str) -> dict:
        return {"accepted": True, "action_key": action_key, "interaction_id": interaction_id}
