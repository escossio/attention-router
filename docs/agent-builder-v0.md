# Agent Builder V0

O Agent Builder V0 cria a fundacao generica para configurar assistentes sem executar nenhuma acao operacional.

## ConfigurationSession

`ConfigurationSession` representa uma entrevista persistente com o usuario. Ela guarda status, estagio atual e respostas estruturadas em JSON. Estados:

- `active`
- `ready_for_review`
- `completed`
- `abandoned`

## AgentBlueprint

`AgentBlueprint` e a identidade estavel do agente configurado. Nesta fase ele nasce apenas como `draft`.

Estados previstos:

- `draft`
- `ready_for_simulation`
- `published`
- `archived`

## AgentBlueprintVersion

`AgentBlueprintVersion` guarda a especificacao imutavel do blueprint. Cada rebuild cria uma nova versao; a versao anterior continua registrada e nao e sobrescrita.

## Autonomia

Niveis conceituais:

- `observe`
- `suggest`
- `approval_required`
- `limited_autonomy`
- `autonomous`

Na V0, blueprints novos sempre iniciam em `observe`. O Builder nao publica agente, nao habilita auto-reply e nao cria execucao autonoma.

## Entrevista

O primeiro interviewer e deterministico: `DeterministicConfigurationInterviewer`. Ele faz uma pergunta por vez e percorre estagios como objetivo, publico, contexto, tom, objetivos, conhecimento, acoes permitidas, limites de aprovacao, proibicoes, escalonamento, autonomia e review.

## Review

Antes do build, o sistema gera um resumo legivel com nome, objetivo, dominio, publico, acoes, limites, informacoes faltantes, autonomia e completude.

## Build

`build_blueprint_from_session()` transforma respostas em `AgentBlueprintSpec` validado por Pydantic. A especificacao distingue:

- `known_information`
- `missing_information`
- `assumptions`

O Builder nao inventa cardapio, precos, horarios ou qualquer dado nao informado.

## Fluxo

```text
User
↓
ConfigurationSession
↓
Interviewer
↓
Structured Answers
↓
AgentBlueprint Draft
↓
Review
↓
Future Simulation
↓
Future Publication
```

## CLI

```bash
python scripts/agent_builder.py start
python scripts/agent_builder.py next --session-id SESSION_ID
python scripts/agent_builder.py answer --session-id SESSION_ID --text "..."
python scripts/agent_builder.py review --session-id SESSION_ID
python scripts/agent_builder.py build --session-id SESSION_ID
```
