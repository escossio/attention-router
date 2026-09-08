# Live Flow tracing schema

## Configuração

- `OTEL_TRACING_ENABLED=false` — fail-safe; sem SDK ativo, o tracer é no-op.
- `OTEL_SERVICE_NAME=attention-router-api` — identificação do processo.
- `OTEL_SERVICE_VERSION=0.1.0` — versão da aplicação.
- `OTEL_EXPORTER_OTLP_ENDPOINT` — endpoint HTTP OTLP opcional; não é necessário para testes.
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` — protocolo candidate suportado.
- `OTEL_RESOURCE_ATTRIBUTES` — pares `key=value` adicionais, filtrados contra nomes sensíveis.
- `OTEL_TRACES_SAMPLER=parentbased_traceidratio` e `OTEL_TRACES_SAMPLER_ARG=1.0` — sampling configurável; 100% somente no candidate sintético.

O provider é configurado em `attention_router/observability/tracing.py`. Aplicação não instancia exporter diretamente.
Quando há exporter OTLP, o provider usa `BatchSpanProcessor` com fila, lote,
timeout e intervalo bounded por configuração. `flush_tracing`/`shutdown_tracing`
são best-effort.

## Attributes permitidos

Os nomes são estáveis e sanitizados:

- `attention.inbound_event_id`, `attention.interaction_id`, `attention.actor_id`, `attention.binding_id`;
- `attention.decision_id`, `attention.execution_intent_id`, `attention.outbox_id`, `attention.correlation_id`;
- `attention.message_type`, `attention.message_from_me`, `attention.message_length`, `attention.message_text_sha256`;
- `attention.response_source`, `attention.response_text_present`, `attention.response_text_length`;
- `attention.final_outcome`, `attention.outcome`, `attention.external_send`;
- atributos específicos de resolução, behavior, repetition, autonomy, memory e outbox descritos em `live-flow.md`.

Texto bruto e credenciais são rejeitados pelo helper `safe_set_attribute`, mesmo que um chamador tente registrá-los.

## Propagação

O inbound injeta W3C Trace Context em metadado de fila somente quando há contexto válido. O worker extrai esse contexto e cria spans filhos/relacionados. A mesma estratégia pode ser usada pelos futuros jobs de Memory com `inject_trace_context`, `extract_trace_context` e `link_from_carrier`.

## Status

Cada span termina em `OK` para outcomes funcionais como `SUPPRESSED`, `BLOCKED` e `NOT_APPLICABLE`; `ERROR` só representa exception funcional. `attention.message` recebe o resumo final quando a camada que abriu o root conhece o resultado.

O backend candidate da Etapa 2 é consultado por trace ID em Tempo; o helper
read-only aplica uma allowlist de attributes para evitar imprimir conteúdo bruto.
