# ADR 0003: Claim Transacional Do Worker

## Status

Aceita.

## Decisão

Timers e outbox usam estados `PENDING`, `PROCESSING`, `DONE`, `FAILED`, `RETRY` e campos de lease.
No PostgreSQL, o claim usa `SELECT ... FOR UPDATE SKIP LOCKED`.

## Consequências

Múltiplos workers podem disputar trabalho sem processar o mesmo item em paralelo. Um item abandonado
em `PROCESSING` volta a ser elegível após o lease.

