# ADR 0010: Fronteira De Autenticação Administrativa

## Status

Aceita.

## Decisão

Usar bearer token administrativo local via `ADMIN_TOKEN`, com `ADMIN_AUTH_ENABLED`.

## Consequências

Control plane fica protegido mesmo em localhost. Antes de exposição pública, será necessário
substituir ou complementar isso com identidade/autorização adequada.
