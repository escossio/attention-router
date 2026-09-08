# ADR 0009: Testes PostgreSQL Reais

## Status

Aceita.

## Decisão

Manter suíte rápida sem PostgreSQL por padrão e criar testes marcados `postgres` que criam um banco
temporário isolado, aplicam Alembic até HEAD e validam locks/constraints/transações.

## Consequências

`pytest -q` segue rápido. `make test-integration` prova comportamentos específicos do PostgreSQL
sem tocar dados reais.

