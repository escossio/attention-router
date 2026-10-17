# Checkpoint — Relay V0 ↔ MAESTRO: homologação integrada

**Registro:** 2026-10-17
**Estado:** CANDIDATO LOCAL / SEM SHA PUBLICADO / SEM CERTIFICAÇÃO POSTGRES DESTE CANDIDATO
**Workspace:** attention-router-relay-maestro-e2e-20261017
**Branch local:** test/relay-maestro-https-e2e-20261017

## Procedência

- PR #368 receptor: branch feat/maestro-relay-contract-v0-20261017, SHA 2c8bfeec22aefc54ff1fbfa068cadbda91e109d5, base do workspace. CI03 histórico: 470 testes PostgreSQL PASS. Esses resultados não certificam este candidato.
- PR #367 Relay: branch feat/zabbix-occurrence-relay-v0-20261017, SHA 696b19bd7a2218ef82d765768c6aee7dba8b6e8e, mais correções locais ainda sem commit no workspace independente. Copiados somente pacote Relay, testes dirigidos e documentação pertinente.
- SHA256 do arquivo local corrigido Relay: b736d162ace8a1fc94a881fc3d7d812027e5ff2c2808da6de282bf168d01d3b1. Isto NÃO é commit nem certificação.

## Contrato integrado pretendido

O Relay aceita somente HTTP 202 ACCEPTED ou 200 DUPLICATE com JSON, receipt_id não vazio e occurrence_number exato. Respostas ambíguas mantêm a outbox pendente.
Cliente TLS valida CA/hostname e aceita CA privada autorizada, sem bypass, redirecionamentos ou proxy herdado.
O MAESTRO reutiliza o ingress existente e registra recibos idempotentes no PostgreSQL. Não ativa rota operacional.

## Resultados locais

- 30/30 testes Python unittest do Relay: PASS.
- Python compileall / py_compile: PASS.
- Ruff E4/E7/E9/F pelo executável existente no CI03, através de stdin e sem carga PostgreSQL: PASS.
- git diff --check: PASS.
- Novo teste integrado de PostgreSQL preparado em tests/integration/test_postgres_maestro_relay_ingress.py: AINDA NÃO EXECUTADO nesta branch.

## Plano do novo teste end-to-end

Com PostgreSQL descartável e app ASGI real por HTTPS loopback:
1. Usar App.dispatch_one real e SQLite temporária; rejeitar CA não confiável mantendo pendência.
2. Repetir dois eventos previamente aceitos: HTTP 200 DUPLICATE, sem inserir novos recibos.
3. Enviar terceiro evento novo e injetar perda sintética da confirmação somente após receber a resposta do servidor e a persistência.
4. Preservar outbox, reabrir SQLite, repetir a mesma ocorrência e receber 200 DUPLICATE, sem quarta linha PostgreSQL.
5. Conservar testes anteriores de contrato, autenticação, conflito e recuperação.

A perda de ACK é simulação controlada pós-persistência: não comprova perda física de pacotes ou disponibilidade de produção.

## Gates para futura homologação

- Revisar diff completo. Não assumir que 30 testes locais equivalem a integração PostgreSQL.
- Depois da aprovação para publicar branch candidata, criar commit e certificar seu SHA exato via CI oficial, incluindo distributed-python, distributed-postgres no CI03, checks públicos e CodeQL.
- Validar a fronteira TLS do receptor sem quebrar o ingresso HTTP existente nem expor Bearer em trecho roteado em claro.
- Sem mudança no AGT operacional, Apache, Zabbix, MikroTik, PostgreSQL real, credenciais, flags, imagens, contêineres, merge ou deploy.
- PRs #367 e #368 permanecem draft até revisão específica.

**Decisão atual:** candidato local preparado; NO-GO para implantação.
