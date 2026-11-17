# Preparação Zabbix 7.4 → Occurrence Relay V0

**Estado:** adaptador JavaScript testado offline. NÃO instalado no Zabbix e NÃO ativado. Nenhuma Action, Media type, usuário/mídia ou credencial foi alterado.

## Estado observado no AGT (leitura)

- Zabbix Server 7.4.14 operacional e PostgreSQL existente preservado.
- Um webhook ativo: Telegram. Uma Action ativa de trigger: CPU/Tempo → Telegram; preservar.
- Nenhum Media type denominado occurrence encontrado.
- Relay V0 stage saudável, acessível somente em loopback do host AGT, sem ligação Zabbix.
- MAESTRO não encontrado como API/contêiner operacional; encaminhamento desligado.
- PR #367, SHA a3edb3cd5aee4817b9a39093e407b391afe512e9: 11 checks PASS na consulta de 17/10/2026.

## Parâmetros do webhook candidato (script no repositório)

| Parâmetro | Macro / origem | Significado |
| --- | --- | --- |
| endpoint | URL HTTPS privada homologada | nunca inventar ou apontar para loopback do Zabbix |
| secret | macro secreta dedicada e protegida | fora do Git, log e corpo |
| event_source | {EVENT.SOURCE} | somente triggers (0) |
| event_id | {EVENT.ID} | identificador do problema |
| event_recovery_id | {EVENT.RECOVERY.ID} | identificador distinto da recuperação |
| event_ts | {EVENT.TIMESTAMP} | instante do problema |
| recovery_ts | {EVENT.RECOVERY.TIMESTAMP} | instante da recuperação |
| host_id | {HOST.ID} | ativo observado, não autoridade tenant |
| trigger_id | {TRIGGER.ID} | código da origem |
| event_name | {EVENT.NAME} | descrição limitada, apenas dado |
| severity | {EVENT.NSEVERITY} | severidade 0–5, não prioridade |

Para PROBLEM, o adaptador produz event_id = problem:EVENT.ID e status = PROBLEM. Para RECOVERY, event_id = recovery:EVENT.RECOVERY.ID e problem_event_id = problem:EVENT.ID, com instante de recuperação. Cada um vira registro próprio no relay, sem encerramento automático.

Eventos UPDATE, descoberta, escalonamento, service events e campos livres ficam fora deste candidato. Não interpretar conteúdo de alerta como instrução. Origem e tenant são vinculados a identidade confiável no receptor, não ao JSON.

## Integração futura e limites

Zabbix permite media type webhook em JavaScript com HttpRequest, parâmetros com macros e operações separadas para Problem/Recovery. Futuramente, criar Media type **dedicado e desativado** inicialmente, com templates apropriados, vincular a mídia de usuário e Action de trigger distinta. A Action Telegram existente permanece intocada.

O relay no estágio atende apenas 127.0.0.1 do AGT, inacessível como loopback do contêiner Zabbix. Antes de qualquer integração viva, decidir o caminho HTTPS aprovado entre containers, credencial e política de acesso. Sem VLAN, bridge, rota, IP estático ou dual-homing novos por conveniência.

A entrega ao MAESTRO não pode ser ligada até que exista uma API real, idempotente e autenticada, com contrato de ACK durável. Falhas devem ficar visíveis sem alterar estados originais no Zabbix.

## Ensaios

- Node: node --test ops/occurrence-relay/zabbix-webhook.test.mjs (sem rede; request mock).
- Python: python3 -m unittest tests.test_occurrence_relay_v0 (sem Zabbix real).
- O HTTP autenticado de ponta a ponta no contêiner está NÃO VERIFICADO; chamada anterior foi bloqueada pelo controle de segurança do plugin, não contornada.
- Atualização e histórico são duas trilhas separadas: live ainda desativado; backfill histórico adiado para evitar contaminar cursores.

## Fontes oficiais

- https://www.zabbix.com/documentation/7.4/pt/manual/config/notifications/media/webhook
- https://www.zabbix.com/documentation/7.4/pt/manual/config/notifications/media
- https://www.zabbix.com/documentation/7.4/pt/manual/appendix/macros/supported_by_location
- https://www.zabbix.com/documentation/7.4/en/manual/config/notifications/action
