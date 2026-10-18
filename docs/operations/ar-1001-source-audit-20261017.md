# AR-1001 — auditoria de origem e próxima coleta Zabbix (V0)

**Data:** 17/10/2026. **Estado:** evidência real consultada somente-leitura; extensão **candidata, não implantada**. Relaciona-se à PR #375 e issue #376.

## Onde o Zabbix recebe a observação

O detector já existe no AGT como `/usr/local/lib/andy-ops-alerts/reply_health.py` (arquivo **host-private**, não mover automaticamente ao GitHub). O timer `andy-reply-health.timer` executa essa coleta periódica. O script consulta o PostgreSQL atual por meio de `docker exec ... psql`, em transação `BEGIN READ ONLY`, e publica por `zabbix_sender` em `Andy Engine - AGT01`.

Há **oito** itens Zabbix reais:

| Chave | Significado do **código-fonte observado**, sem interpretação adicional |
| --- | --- |
| `andy.reply.probe_ok` | 1 quando a consulta e a análise da sonda passam; zero quando falha |
| `andy.reply.functional_stalled` | estado persistente do incidente; não normaliza por mera ausência de nova falha |
| `andy.reply.direct_6h` | **interações distintas** elegíveis pelo filtro de `source=wwebjs` e sufixo do `actor_id` dentro da janela de 6 horas; não é contador de todas as mensagens |
| `andy.reply.no_safe_6h` | interações distintas com evento de auditoria `behavior.unavailable` |
| `andy.reply.blueprint_6h` | interações distintas com evento de auditoria `conversation.blocked`; **não** é prova independente de erro de blueprint |
| `andy.reply.other_failed_6h` | interações distintas com as demais classes de falha de auditoria do filtro |
| `andy.reply.latest_failure_epoch` | instante Unix da ocorrência de auditoria mais recente classificada como falha |
| `andy.reply.last_agent_done_epoch` | último `outbox DONE` correspondente a resposta automática de interação direta, no histórico consultado |

A sonda pode manter `functional_stalled=1` enquanto não ocorrer resposta automática DONE **mais recente que a abertura e a última falha**. Por isso a persistência de PROBLEM no Zabbix não é uma nova mensagem nem um novo disparo. `probe_ok=1` garante que a sonda consultou; não é prova de recuperação da Andy.

## Auditoria read-only efetivamente realizada

Janela de seis horas no PostgreSQL operacional, sem ler corpos de mensagem, nomes, IDs de interação ou destinos:

| Propriedade observada | Valor |
| --- | ---: |
| Interações diretas distintas | 6 |
| Entradas `wwebjs` dessas interações, processadas | 6 de 6 |
| Decisões `INSUFFICIENT_CONTEXT / DRY_RUN` dessas interações | 5 |
| Auditorias `behavior.unavailable` | 5 |
| Auditorias `conversation.blocked` | 5 |
| Mensagens novas de outbox nessas interações/janela | 0 |

Houve também uma janela de espera cancelada e quatro liberadas, observadas por consulta de `conversation_response_grace_windows` com filtro de fonte/janela. **Não** atribuir causalidade automática a `DRY_RUN`, `conversation.blocked` ou ausência de outbox: alguns silêncios podem ser intencionais, parte das janelas pode ter sido cancelada e o critério de resposta obrigatória ainda não é comprovado.

## Lacuna comprovada e menor extensão possível

A consulta `ops/observability/failure-catalog/queries/ar-1001-readonly-progress.v0.sql` amplia **somente agregados de contagem** a partir das mesmas tabelas e IDs relacionais do detector; não acessa mensagens completas nem retorna identificadores reais. É candidata a evoluir a sonda existente, não um coletor novo.

Campos novos *propostos para o Zabbix*, sempre em janela seis horas:
- `andy.reply.direct_processed_6h`: entradas diretas cujo processamento foi confirmado.
- `andy.reply.decision_total_6h` e `andy.reply.decisions_dry_run_6h`: decisões de entradas diretas e subconjunto `INSUFFICIENT_CONTEXT/DRY_RUN`.
- `andy.reply.outbox_new_6h`: novas linhas de outbox de interações diretas.
- `andy.reply.grace_released_6h` e `andy.reply.grace_canceled_6h`: janelas de resposta ligadas às interações, por estado.

**Não** somar `no_safe_6h + blueprint_6h` como total de mensagens: a mesma interação pode aparecer em ambas as categorias. E nunca presumir que `direct_6h = resposta obrigatória`: critério de elegibilidade e janela da política precisam de contrato próprio.

O SQL é marcado como `BEGIN TRANSACTION READ ONLY` e `SET LOCAL statement_timeout = '3s'`, mas **sua execução a partir do arquivo foi bloqueada pela ferramenta remota neste turno**. Não afirmar ensaio SQL dessa versão nem item instalado. Os agregados anteriores foram obtidos por consultas SELECT diretas de leitura. A consulta candidata ainda exige teste sintético e validação de sintaxe/latência no banco autorizado antes de qualquer mudança em produção.

## Ordem operacional para a próxima passada

1. Comparar `ar-1001-readonly-progress.v0.sql` com os agregados já medidos, teste SQL em banco descartável e medição de latência; manter `probe_ok` e estado do incidente atual intocados.
2. Versionar na origem dos itens Zabbix os contadores adicionais e seus tipos `uint`, somente após backup exportado do template/host; usar a mesma sonda e o mesmo destino `zabbix_sender`, sem duplicar scheduler.
3. Testar em dados sintéticos os casos: 0 entradas, entradas processadas sem resposta exigível, decisão de bloqueio intencional, ausência de outbox, caminho de entrega e sonda/DB indisponível.
4. Acrescentar visibilidade MGMT com rótulos e unidades corretos e preservar Telegram/MAESTRO existentes. Só então avaliar tag `fault_code=AR-1001` para **novos** eventos.
5. Verificar cobertura cruzada Zabbix/OSI/OTel/GoAccess/Dozzle e registrar `NOT_OBSERVED` quando o fluxo não puder ser correlacionado. Não pressupor falha ou saúde de camada inferior.
6. Evoluir contrato Zabbix → Relay **depois** de consolidar as evidências na origem e aceitar nova versão. Não enviar os campos V1 para o webhook estrito atual.

**Escopo desta entrega:** consulta de inventário, SQL proposto, documentação e testes offline. Nenhuma alteração ao script host-private, timer, PostgreSQL, mídia Zabbix, MQTT/WhatsApp, Relay ou MAESTRO.
