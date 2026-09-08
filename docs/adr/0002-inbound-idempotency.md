# ADR 0002: Idempotência De Entrada

## Status

Aceita.

## Decisão

Registrar cada evento recebido em `inbound_events` com `source`, `external_event_id`, payload
normalizado, hash e constraint única em `(source, external_event_id)`.

## Consequências

Replay sequencial ou concorrente retorna a interação já produzida. A garantia principal é do banco,
não de um `SELECT` prévio.

