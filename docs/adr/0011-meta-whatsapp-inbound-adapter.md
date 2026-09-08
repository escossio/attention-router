# ADR 0011: Meta WhatsApp Inbound Adapter

Data: 2026-08-07

## Decisão

Meta/WhatsApp entra pelo listener físico dedicado `attention-router-ingress`, exposto apenas em localhost. Esse listener contém somente health mínimo e `GET|POST /api/v1/ingress/meta/whatsapp/webhook`.

O payload Meta é tratado exclusivamente por `MetaWhatsAppInboundAdapter`, que produz `NormalizedInboundEvent` schema `1`. O core não conhece `entry`, `changes`, `messages`, `statuses`, `wa_id`, `wamid` ou `phone_number_id`.

## Lote

`InboundAdapter` agora aceita `normalize_many`. Um POST Meta pode conter múltiplas mensagens; cada mensagem vira um evento lógico independente. Status callbacks são auditados, mas não criam interação.

## Identidade

O adapter fornece `source=meta_whatsapp` e o identificador externo do ator. A resolução para identidade lógica interna ocorre por `ActorBinding`, fora do adapter. Sem binding ativo, o ator segue para política de desconhecido.

## ACK

Conflito de mesmo ID Meta com payload diferente é auditado e recebe ACK HTTP 200 para evitar retry infinito do provedor. Falhas de banco antes de persistência durável continuam podendo retornar erro.

## Outbound

Esta fase não implementa envio WhatsApp. Outbox existente continua mock/futuro.
