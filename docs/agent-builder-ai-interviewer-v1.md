# Agent Builder AI Interviewer V1

Esta fase adiciona um entrevistador com LLM para o Agent Builder sem conectar o blueprint ao runtime.

## Provider Abstraction

`ConfigurationInterviewer` continua existindo para o fluxo deterministico. A camada de IA fica separada:

- `DeterministicConfigurationInterviewer`
- `OpenAIConfigurationInterviewer`

A selecao usa `AGENT_BUILDER_INTERVIEWER_PROVIDER=deterministic|openai`. O default e `deterministic`.

## Structured Output

O provider OpenAI usa Responses API com Structured Outputs por JSON Schema estrito baseado no Pydantic `InterviewerTurn`.

O LLM retorna um turno estruturado com:

- `assistant_message`
- `next_question`
- `proposed_updates`
- `new_missing_information`
- `resolved_information`
- `ready_for_review`
- `needs_clarification`
- `confidence`
- `reason_codes`

Texto livre nao e fonte de verdade para atualizar a sessao.

## Patch Allowlist

A IA so pode propor `BlueprintPatch` em caminhos explicitamente permitidos:

- `purpose`
- `domain`
- `audiences`
- `tone`
- `goals`
- `knowledge_requirements`
- `rules`
- `constraints`
- `actions.allowed`
- `actions.requires_approval`
- `actions.forbidden`
- `escalation`
- `success_criteria`

Patches para status publicado, autonomia operacional, ids internos, banco, outbox, transporte ou secrets sao rejeitados pela aplicacao.

## Seguranca

Prompt injection em documentos colados pelo usuario e tratado como dado da configuracao. A aplicacao nao permite que o modelo publique agente, execute acao, crie outbox ou altere transporte.

## Autonomia

A IA pode conversar sobre autonomia e registrar preferencias como dado, mas a autonomia efetiva do blueprint continua `observe`.

## Fallback

Se o provider falhar e `fallback_to_deterministic=true`, a resposta do usuario e preservada e o fluxo deterministico assume o proximo passo.

## Erros

Sao tratados:

- timeout
- rate limit
- auth failure
- invalid structured response
- refusal
- incomplete response
- network error

Falhas sao auditadas sem payload completo e sem API key.

## Privacidade

O contexto enviado ao LLM e compacto: objetivo, respostas relevantes, resumo estruturado, informacoes faltantes, estagio atual, ultima resposta e allowlist de campos. Nao envia banco inteiro, logs, contatos reais, JIDs, secrets ou conversas de WhatsApp.

## Fluxo

```text
User
↓
ConfigurationSession
↓
AIConfigurationInterviewer
↓
InterviewerTurn validado
↓
Patch allowlist
↓
Structured Answers
↓
Review humano
↓
AgentBlueprint Draft versionado
```
