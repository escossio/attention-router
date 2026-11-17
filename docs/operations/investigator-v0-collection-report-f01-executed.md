# Relatório de Coleta 003 — F01 reproduzido no PostgreSQL descartável

**Janela de governança:** [#356](https://github.com/escossio/attention-router/issues/356) · **Caso:** F01 / AR-INV-GAP-001 · **PR de testes:** [#357](https://github.com/escossio/attention-router/pull/357) · **Base do motor:** [#355](https://github.com/escossio/attention-router/pull/355).
**Estado:** COLETA FOCAL CONCLUÍDA EM FIXTURE; questão de semântica de retry EM REVISÃO; NO-GO para produção.
**Data:** 2026-10-15 (America/Fortaleza); execução do artefato em UTC conforme nome do arquivo.

## 1. Propósito do ensaio

Observar o que ocorre quando uma assinatura HMAC sintética válida é admitida, o nonce é consumido em memória, mas a execução falha **antes da chamada que escreve seed/nonce/audit no PostgreSQL**. O objetivo não foi corrigir ou tentar vulnerar a proteção, e sim provar o estado das duas fronteiras e produzir relatório nominal.

### SHA e integridade

- Branch isolada: `test/investigator-v0-f01-precommit-20261015`.
- SHA executado pelo próprio worker: **`b6fc9e1f26be53cfb25e473c2d3e78613e5047c1`** (40 caracteres).
- SHA da base #355: **`ac67ec00a4145f3b35a56576523b27ded5044d62`**.
- Diff verificado na COL-016: **somente um arquivo de testes**, 75 inserções e 0 remoções; nenhuma alteração no código da admissão HMAC, gateway ou store PostgreSQL.
- Dispositivo controlador: AGT (somente proxy SSH/controle). Execução do pytest e contêiner PostgreSQL **apenas no CI03**, escolhido pelo Host Registry.

## 2. Protocolo executado

Preflights documentados COL-021–023 comprovaram compatibilidade do executor, conexão read-only ao worker, lock livre e ausência de contêiner PostgreSQL CI ativo antes do experimento. O comando exato foi registrado **antes de executar**, em COL-024 na issue #356.

O executor `andy-ci-run` do CI03 foi iniciado uma única vez no SHA acima, com `ANDY_CI_PYTEST_TARGETS_FILE` contendo **somente duas node IDs** do arquivo existente `tests/integration/test_postgres_investigator_durable_admission_v0.py`; `PYTEST_ADDOPTS` incluiu relatório nominal `-rxX` e `--junitxml`. Foi utilizada a fixture PostgreSQL descartável do repositório, não o banco operacional.

## 3. Resultados nominais observados

| Teste | Resultado | Significado comprovado neste laboratório |
| --- | --- | --- |
| `test_f01_after_hmac_before_db_consumes_nonce_without_durable_seed` | **PASS** | Injeção de falha pré-DB em fixture: nenhuma linha persistida; nonce já consumido na mesma instância; nova instância pode admitir o mesmo envelope sintético |
| `test_f01_required_same_instance_retry_after_abort_is_recoverable` | **XFAIL** (strict) | A proposta de recuperação por reutilização do MESMO envelope/nonce não foi atendida; **não é política antirreplay ratificada** |

**Saída observada:** `1 passed, 1 xfailed in 1.75s`. Executor: `CI_STATUS=PASS`, `CI_DURATION_SECONDS=9`, `F01_RUNNER_RC=0`, `F01_WRAPPER_RC=0`, `F01_CASE_COUNT=2`.

**Artefato:** `f01-b6fc9e1f26be-20261015T195021Z-3195417.xml`, armazenado somente em `/srv/andy-ci/results` no worker CI03, sem download/publicação bruta no GitHub.
**SHA-256 dos bytes efetivamente coletados:** `197e04ba9ebf2e4ebdc1da5f7538853c2681208eea9e53aea272def1aad92f0e`.

A análise do XML verificou explicitamente as duas entradas `<testcase>` e o resultado `XFAIL` através do nó `<skipped type='pytest.xfail'>`. Não inferir esses resultados exclusivamente do sumário anterior de 476 testes da CI.

## 4. Verificação de limpeza

A COL-025 executou **somente leitura** no CI03, após a execução:

| Indicador | Observação |
| --- | --- |
| `F01_JUNIT_DIGEST_VALID` | YES |
| `F01_JUNIT_MATCHES` | 1 |
| `F01_TARGETS_LEFT` | 0 |
| `F01_PG_CONTAINERS_ACTIVE` | 0 |

Esses valores representam o instante da inspeção e não garantem ausência permanente de outras atividades de CI. Nenhum contêiner foi removido manualmente; o próprio runner limpou seus recursos temporários.

## 5. Interpretação, hipóteses e limitações

**CONFIRMADO EMPIRICAMENTE EM FIXTURE:** existe separação entre a reserva `_seen.add(token)` em memória e a transação do banco. Sob a falha F01 antes de `stage_verified`, o PostgreSQL permanece vazio, mas o nonce já é considerado usado na instância que recebeu a requisição.

**NÃO CONCLUÍDO:** que o mesmo nonce deva ser reaceito. Bloqueio de replay é uma propriedade de segurança importante. A decisão precisa buscar **recuperação autenticada e idempotente** sem permitir que o mesmo envelope produza outra sessão.

**NÃO TESTADO NESTA EXECUÇÃO:** falha física do PostgreSQL; commit confirmado com ACK perdido; concorrência distribuída sob novas políticas; autenticação Zabbix real; tenants reais; auditoria durável de todas as consultas/hipóteses/conclusões; custo de modelo; integração ao AGT.

**Semânticas candidatas a avaliar separadamente (sem aprovação):** (A) novo nonce e nova assinatura preservando a identidade do evento; (B) consulta autenticada de status/recibo; (C) reserva transacional com estado explícito de recuperação. Nenhuma alternativa é autorizada como implementação por este relatório.

## 6. Decisão e próximos gates

**F01 OBSERVACIONAL: PASS / GAP DE CONSISTÊNCIA REPRODUZIDO NO LABORATÓRIO.**
**PROPOSTA DE RETRY COM MESMO NONCE: XFAIL / SEM DECISÃO NORMATIVA.**
**PRs #355 e #357: rascunhos; sem merge.**
**GO somente para revisão de contrato/ADR de recuperação; NO-GO para modelo real, implantação no AGT, acesso a sistemas operacionais ou remediação automática.**

Qualquer outro ensaio requer ficha prévia própria na #356 com SHA, alvo, orçamento, mecanismo de parada, autorização e resultado pós-execução. O histórico não será alterado para apagar falhas.

## 7. Cadeia de registros

- Issue de janela #356: COL-021 (preflight de caminho), COL-022 (AGT read-only), COL-023 (CI03 read-only), COL-024 (comando completo e saída nominal), COL-025 (digest e limpeza).
- PR #357: diffs de teste F01 e resultado nominal comentados, sem mudanças no motor.
- PR #344: este relatório documental, separado dos dados brutos de CI.

**Regra aplicada:** justificar antes, executar somente o escopo aprovado, verificar, registrar, e só então decidir.