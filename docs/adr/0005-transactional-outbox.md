# ADR 0005: Transactional Outbox

## Status

Aceita.

## Decisão

Cada tentativa de ação cria uma mensagem em `outbox_messages` na mesma transação da decisão e do
timer. O worker processa somente adapter mock nesta fase.

## Consequências

O núcleo fica pronto para adaptadores reais sem acoplar chamadas externas à transação principal.
`idempotency_key` impede duplicidade lógica futura.

