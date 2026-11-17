# MAESTRO — contrato mínimo de recebimento do Relay V0 (gate, não implementação)

**Estado:** PROPOSTA EM REVISÃO, não é endpoint existente, não cria MAESTRO e não autoriza deploy. Escopo: definir a ponta que falta para ativar o contêiner mínimo Zabbix → Relay → MAESTRO sem recriar a orquestração.

## Evidências da consulta

A branch documental docs/maestro-incident-control-plane-v0-20261016 descreve MAESTRO como papel arquitetural, sem demonstrar API executável. A busca na main do repositório e o inventário de contêineres do AGT não identificaram um serviço MAESTRO. O Relay estágio permanece com maestro_configured=false. A ausência de contêiner não é prova geral de ausência de código ou deployment remoto; para este estágio, não existe endereço/contrato de receptor certificado.

O Zabbix 7.4.14 tem webhook habilitado de Telegram e Action ativa de CPU/Tempo. Ambos devem permanecer inalterados. O webhook adicional versionado no PR #367 é apenas candidato offline.

## Mensagem transmitida pelo Relay (implementação atual)

**Iniciador:** Relay. **Destino:** futura API autenticada do MAESTRO, sem endereço definido. **Transporte:** HTTPS com TLS validado. O código atual envia objeto JSON neste formato ilustrativo, gerado a partir de dados sintéticos:

~~~json
{
  "contract_version": "occurrence-relay-v0-draft",
  "occurrence_number": 1,
  "source_system": "zabbix",
  "tenant_id": "synthetic-tenant",
  "signal": {
    "schema_version": "1",
    "event_id": "problem:10123",
    "occurred_at": "2026-10-17T12:00:00Z",
    "asset_ref": "zabbix:host:2001",
    "signal_code": "ZBX_TRIGGER:333",
    "status": "PROBLEM",
    "severity": 4
  }
}
~~~

Cabeçalhos candidatos: Content-Type application/json; Authorization Bearer de serviço de saída, diferente do token de entrada; Idempotency-Key composta por source_system + número local, vinculada ao tenant/identidade autenticados.

**O MAESTRO não pode confiar no campo tenant_id enviado pelo Relay como prova de autoridade.** Deve derivar ou validar tenant/source contra a identidade autenticada da ligação. IDs numéricos são locais ao Relay e não são identidades globais ou autorização.

## Condições para o ACK do destinatário

1. Validar credencial do serviço, escopo, formato e versão sem coerção, preservando payload como dado não confiável.
2. Registrar/conciliar a identidade idempotente (tenant/ligação/fonte + ocorrência e versão); replay idêntico deve retornar recibo equivalente, sem duplicar trabalho.
3. Só enviar HTTP 2xx quando o evento estiver **duravelmente aceito** ou for duplicado idêntico já aceito. Caso temporariamente indisponível, retornar erro apropriado para deixar a entrega pendente.
4. Mesmo idempotency key com bytes semanticamente conflitantes deve falhar e ficar auditável, não atualizar silenciosamente.
5. Recuperação enviada como novo evento com problem_event_id explícito não implica fechamento automático de uma investigação.
6. Resposta de MAESTRO **não autoriza o Relay a executar correção**. Relay não define prioridades, não interpreta causa, não cancela trabalho.
7. Endpoint deve ter contrato de retry, retentativa após perda de resposta, efeitos duplicados e garantia de persistência; só então os sucessos HTTP podem ser reconciliados com outbox local.
8. A execução atual do Relay marca entregue ao receber 2xx; essa regra depende estritamente do contrato 2xx do destino, que ainda não existe.

## O que ainda precisa ser descoberto

- Qual entidade/código realmente implementará o receptor MAESTRO (não criar outro contêiner por conveniência).
- Rota, método, versão estável, porta e identidade TLS; não inventar endereço ou serviço.
- Fonte canônica do tenant, vínculo de credencial por instalação, autorização de evento.
- Contrato de recusa e código de aceite, idempotência e consistência transacional.
- Rede mínima Zabbix → Relay → MAESTRO sem dual-homing ou alteração de VLAN injustificada.
- Teste sintético da perda de ACK: `tests/test_occurrence_relay_maestro_ack_v0.py` já comprova, em receptor **simulado** com estado em memória, que o Relay conserva a pendência e reenvia a mesma chave após restart, sem duplicar trabalho no simulador. A prova real de idempotência e persistência do MAESTRO continua pendente.
- Política de retenção e backup/restore da outbox/SQLite e integração com histórico canônico.

**Decisão operacional:** manter MAESTRO_URL desabilitado no estágio. Não instalar webhook Zabbix, não conectar origem real, não expor relay fora do loopback. Não fazer merge automaticamente. O próximo gate depende de localizar/aprovar o contrato real do MAESTRO.

Live continuity: nada muda nos eventos de Zabbix operacional. Historical acceleration: importação antiga não habilitada; cursores e estado live preservados.
