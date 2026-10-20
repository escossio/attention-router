# AR-1002 — Visitor Readiness (contrato V0, offline)

**Estado:** proposta implementada em branch isolada; sem coleta real, sem integração Zabbix e sem implantação. Complementa AR-1001; não substitui a verificação de resposta real.

## Propósito

Detectar perda do contrato de atendimento textual a visitante externo direto e sem binding, independentemente da existência de mensagens recentes. Reutilizar o guard do pacote de recuperação certificado na PR #380 (SHA `4b5c1eaa2ffee3e977cca993e0089e8867d967a6`), sem criar novo motor de autorização.

## Redutor puro

`attention_router/observability/visitor_readiness.py` aceita evidências tipadas booleanas e retorna `READY`, `BROKEN`, `DEGRADED` ou `UNKNOWN`. A suíte `tests/test_visitor_readiness.py` usa exclusivamente dados sintéticos.

- `READY`: todas as propriedades obrigatórias comprovadas por snapshot consistente e coletor disponível.
- `BROKEN`: propriedade obrigatória comprovadamente falsa em snapshot consistente.
- `DEGRADED`: propriedade ausente ou indeterminada; não é autorização para inferir saúde.
- `UNKNOWN`: fonte inacessível, snapshot inconsistente ou janela de manutenção. Um erro de coleta nunca produz um falso OK.
- `configuration_only=true`: não certifica chamada de modelo, outbox ou ACK WhatsApp.

O redutor verifica presença de default, flag e guard, versão/publicação/checksum, isolamento de tenant e policy textual sem capabilities/disclosures. A origem de dados ainda não foi implementada.

## Gate para coletor futuro

Antes de integrar ao Zabbix, identificar no runtime a fonte autorizada para cada propriedade, como verificar o SHA do guard efetivo e como obter um snapshot de banco **READ ONLY** consistente. Nunca tratar a simples presença de uma flag como prova de integridade da policy.

Reutilizar o ciclo existente de `andy-reply-health.timer`, mas manter falha de coleta separada de violação comprovada. Adicionar itens/triggers e código ao catálogo só depois de testes de frescor, concorrência de deploy, rollback e ausência de regressão do AR-1001.

Nenhuma mensagem sintética externa, execução de agente, mudança de blueprint, escrita no PostgreSQL, restart, replay ou publicação operacional faz parte deste V0.
