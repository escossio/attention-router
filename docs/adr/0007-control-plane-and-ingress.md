# ADR 0007: Control Plane E Public Ingress

## Status

Aceita.

## Decisão

Separar rotas administrativas em `/api/v1/admin/...` e entrada futura em `/api/v1/ingress/...`.
Rotas antigas ficam deprecated e protegidas por autenticação administrativa.

## Consequências

A futura exposição pública deve apontar apenas para ingress. Políticas, auditoria, simulador e
diagnóstico pertencem ao control plane restrito.

