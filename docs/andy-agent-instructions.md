# Andy Agent

O caminho agentic usa um único agente, `ANDY`, dentro do worker. O Agents SDK produz somente uma proposta estruturada; policy, contexto permitido, repetition guard, autonomia, review, execution, outbox e transporte continuam sob autoridade do Router.

Flags principais:

- `ANDY_AGENT_ENABLED=false` por padrão;
- `ANDY_AGENT_MODEL=gpt-5.6-sol`;
- `ANDY_AGENT_MAX_TURNS=3`;
- `EXTERNAL_DELIVERY_ENABLED=false` no canário inicial.

Falha, timeout ou output inválido do agente resulta em `AGENT_FAILURE`/HOLD, sem fallback externo silencioso. O contexto é construído pelo Router e a memória canônica continua no PostgreSQL; nenhuma Session do SDK é fonte de verdade.
