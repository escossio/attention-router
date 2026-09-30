# Attention Router Live Flow — Etapas 1 e 2

Etapa 1 adiciona instrumentação OpenTelemetry offline ao fluxo Python. Etapa 2
adiciona a stack candidate isolada Collector→Tempo. Etapa 3 adiciona Grafana
somente como interface read-only para essa mesma fonte; não há lógica funcional
nem alteração do canário vivo. `OTEL_TRACING_ENABLED=false` é o padrão e o
runtime atual permanece desligado.

A topologia e os procedimentos da stack estão em
[`tempo.md`](tempo.md). O candidate aponta para Collector, nunca diretamente
para Tempo.

A interface humana está documentada em
[`grafana-message-journey.md`](grafana-message-journey.md). Grafana consulta
Tempo por `http://tempo:3200` na rede Docker e é publicado apenas em
`http://192.0.2.6:3000` (acessível somente na rede AGT `192.0.2.4/24`).

## Trace root

Uma nova mensagem entra em `attention.message`. O caminho síncrono cria `inbound.receive` e, quando alcançadas, as etapas `actor.resolve`, `policy.resolve`, `decision.evaluate`, `behavior.generate` e `repetition_guard.evaluate`. O worker recebe o contexto W3C em metadado não funcional de `QueueRow` e cria spans de continuação com o mesmo trace quando essa propagação está habilitada.

Esta base contém Persistent Memory e Conversation Repetition Guard. Os spans `memory.archive`, `memory.extract` e `repetition_guard.evaluate` são emitidos somente nos hooks reais, quando cada etapa é alcançada; não há spans sintéticos no fluxo live. Se uma etapa estiver desabilitada ou não for alcançada, ela permanece ausente/`NOT_REACHED`.

## Taxonomia

| Span | Resultado observado |
|---|---|
| `attention.message` | resumo final: `DELIVERED`, `SUPPRESSED`, `BLOCKED`, `FAILED` ou `NO_RESPONSE` |
| `inbound.receive` | aceitação, replay ou conflito de payload |
| `actor.resolve` | `BOUND` ou `UNKNOWN` |
| `policy.resolve` | policy/version selecionada ou fallback |
| `decision.evaluate` | tipo, ação recomendada, confiança e informação faltante |
| `behavior.generate` | `andy_behavior`, `legacy` ou `NONE` |
| `repetition_guard.evaluate` | mudança de estado, repetição e supressão |
| `autonomy.evaluate` | modo efetivo e autorização |
| `execution.intent` | criação/status do intent |
| `outbox.enqueue` | criação e quantidade de outbox |
| `transport.send` | lote enviado pelo adapter Python |

`SUPPRESSED` e `BLOCKED` são resultados funcionais normais, não erros de telemetria. Etapas não alcançadas não recebem spans.

## Privacidade

O primeiro gate de hardening restringe atributos e Resources às allowlists e aos
validadores de valores descritos em [`tracing-schema.md`](tracing-schema.md).
Comprimentos, presença, enums operacionais e referências internas UUID permitidas
continuam disponíveis. Texto bruto, objetivos/raciocínio de IA, hashes de
conteúdo/identidade, PII e credenciais são descartados.

## Falhas e async

Exceções funcionais recebem somente `error.type` allowlisted, outcome `FAILED` e
status `ERROR` sem descrição. Não há captura completa ou evento duplicado de
exceção. Falhas de telemetria preservam o resultado/erro funcional original.
`traceparent` válido pode ser injetado em metadata de job; extração inválida usa
contexto vazio e abre novo trace local sem herdar outra operação. Exportação HTTP
e flush têm timeouts separados; ver [`tracing-schema.md`](tracing-schema.md).

O adapter Node/WhatsApp não é alterado nesta etapa. A correlação disponível até a fronteira é o `correlation_id` da outbox e o contrato de propagação; `traceparent` atravessando o protocolo do transport fica para a Etapa 2.

## Demonstração

```bash
OTEL_TRACING_ENABLED=false .venv/bin/python scripts/trace_demo.py
```

O script usa somente `InMemorySpanExporter` e imprime IDs de trace e outcomes sanitizados.

Para o backend da Etapa 2:

```bash
docker compose -p attention-router-live-flow -f compose.observability.yaml up -d tempo otel-collector
OTEL_TRACING_ENABLED=true .venv/bin/python scripts/trace_demo.py --otlp
```

O trace retornado pode ser consultado de forma read-only com
`scripts/tempo_trace.py <trace_id>`.

Durante uma janela controlada, `scripts/live_flow_watch.py --next --since <unix>
--binding <binding-hash> --timeout 600` consulta Tempo em foreground, exibe
somente nomes de spans/outcomes sanitizados e encerra no primeiro trace
compatível. O watcher não escreve no banco, não aprova intents e não envia
mensagens.
