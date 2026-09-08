# Incremental memory

## Fluxo

`InboundEvent/Interaction -> conversation archive -> PENDING memory job -> worker extraction -> candidate -> claim/evidence -> ActorMemoryContext -> Behavior`.

O archive usa a mensagem persistida como fonte canônica e não cria inbound adicional. A mesma chave externa não cria segunda linha. `from_me` é capturado apenas como contexto; self-loop protection encerra o evento antes de criar interaction ou Decision.

## Flags

- `PERSISTENT_MEMORY_ENABLED=false`: captura archive, opcionalmente limitada por `PERSISTENT_MEMORY_CANARY_BINDING_ID`.
- `MEMORY_INGESTION_ENABLED=false`: enfileira/processa extração assíncrona.
- `MEMORY_CONTEXT_ENABLED=false`: impede uso pelo Behavior, mesmo que exista memória.

Todas são fail-closed. O canário é por binding, não por nome/telefone hardcoded.

## Sessões e isolamento

Conversation state transitório pode ser resetado sem apagar actor, claims ou evidence. O lookup é por actor canônico, então memória não cruza actors. Display name observado permanece separado de nome autodeclarado e tratamento preferido.

O capture de memória é side-channel e não obriga uma resposta. O `ConversationRepetitionGuard` bloqueia respostas repetidas sem mudança de estado antes do outbox; a chegada de uma nova intenção, slot, pergunta ou fase conversacional permite nova resposta.

## Falhas e privacidade

Archive/extraction é side-channel: erro é auditado e o fluxo normal continua. Claims temporais novas supersedem as ativas sem apagar histórico. Segredos são redigidos no archive e nunca promovidos/indexados; memória sensível só chega ao Behavior quando o contexto permitir.

## Estado operacional

O adapter de histórico/backfill continua `DISABLED / FAIL-CLOSED`; `WHATSAPP_HISTORICAL_BACKFILL` é backlog não bloqueante. Este documento cobre somente mensagens novas e dry-runs offline.
