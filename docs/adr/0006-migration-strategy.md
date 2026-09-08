# ADR 0006: Estratégia De Migração

## Status

Aceita.

## Decisão

Usar Alembic como único mecanismo de evolução de schema. `create_all` fica restrito a testes
unitários. O startup da API executa `alembic upgrade head`; workers dependem da API saudável para
evitar corrida de migrations.

## Consequências

Banco vazio e banco existente evoluem pelo mesmo caminho. Antes de upgrades em ambiente com dados,
fazer backup lógico.
