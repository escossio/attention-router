# Supervisão funcional de respostas da Andy no WhatsApp

## Objetivo

Sinalizar a falha funcional observada: mensagens individuais chegam e
são processadas, mas a Andy não produz respostas automáticas por dias.
Não usar saúde de container, HTTP 200 ou evento de iframe como substitutos
da entrega funcional. Não alterar o funcionamento da Andy.

## Entradas e origem

- PostgreSQL de produção, somente `BEGIN READ ONLY`
- `inbound_events` com origem `wwebjs` e destinatários individuais
  `@lid` / `@c.us`; conversas em grupos não entram na regra
- `audit_events`: `behavior.unavailable`, `conversation.blocked`
  e demais falhas associadas às interações diretas
- `outbox_messages`: somente
  `agent_execution_text` ou `agent_execution_voice`, destinos
  `local_transport` e `wwebjs`, `DONE`, `completed_at` preenchido;
  vinculados a uma mensagem direta recebida
- Não coleta texto das mensagens, destinatários, telefones, tokens ou
  conteúdo de `payload`; exporta apenas contagens, timestamps e estado.

## Contrato do alarme

Abre `ANDY_WA_AUTO_REPLY_STALLED` se:
- Pelo menos 3 interações individuais nas últimas 6 horas;
- Pelo menos 2 eventos de falha nas interações, com contagem por categoria;
- O evento de falha mais recente é posterior à última entrega automática
  concluída (ou não existe entrega anterior).

**Uma vez aberto, permanece aberto.** Ausência de novas mensagens, passagem
do tempo e respostas manuais NÃO são prova de recuperação.

Fecha APENAS se existir uma nova entrega automática concluída, com
`completed_at` maior que a abertura do incidente e maior que a última
falha da janela. Exigir observação factual, jamais sintética.

Alarme adicional `ANDY_WA_REPLY_OBSERVABILITY_LOST` sinaliza indisponibilidade
da própria consulta de evidências (falha de query ou ausência de métricas).
Nenhum erro de consulta pode virar falso estado `OK`.

## Frequência e saída

`andy-reply-health.timer`: a cada 2 minutos no AGT01.

- `andy.reply.probe_ok`
- `andy.reply.functional_stalled`
- `andy.reply.direct_6h`
- `andy.reply.no_safe_6h`
- `andy.reply.blueprint_6h`
- `andy.reply.other_failed_6h`
- `andy.reply.latest_failure_epoch`
- `andy.reply.last_agent_done_epoch`

Zabbix ROC, host `Andy Engine - AGT01`, triggers com tag `ops_alert`
alimentam a ação Telegram #7 já certificada com PROBLEMA e RESOLVIDO.
Estado do detector no arquivo privado
`/midia/agt01-storage/attention-router/private/andy-reply-health-state.json`
(root, 0600).

## Diagnóstico inicial 15/10/2026

- Respostas automáticas de `agent_execution_text/voice`: última entrega
  em 05/10/2026.
- 41 interações individuais em seis horas durante a instalação,
  33 eventos `BEHAVIOR_UNAVAILABLE_NO_SAFE_RESPONSE` e
  33 `CONVERSATION_BLUEPRINT_UNCONFIGURED`.
- Trigger #32704 em PROBLEMA, alerta #5 enviado por Telegram, sem erro.
- Serviços de transporte/browser saudáveis, apesar da ausência de respostas.

## Verificações e rollback

Inspecionar:
`systemctl status andy-reply-health.timer`;
`journalctl -u andy-reply-health.service -n 20 --no-pager`;
painel ROC/Zabbix e Telegram.

Antes de qualquer correção da Andy, preservar as evidências do incidente.
Após a correção, executar uma interação legítima e verificar a entrega
automática concluída e o evento Zabbix RESOLVIDO no Telegram.

Para interromper APENAS a coleta de observabilidade, desabilitar o timer;
o alarme de perda de observabilidade deve continuar sinalizando ausência de
dados. Não apagar o estado persistido nem redefinir o trigger para encerrar
artificialmente o incidente.

O detector é baseado nas falhas observadas hoje e tem limitação:
não certifica indisponibilidade quando não há demanda e ainda não possui
um teste sintético de resposta de ponta a ponta. Esse será um gate adicional,
com total isolamento e sem mensagens espontâneas em conversas privadas.
