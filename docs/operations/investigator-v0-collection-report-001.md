# Relatório de coleta 001 — Investigador Operacional V0

**Janela:** `INV-V0-COLLECT-20261015` · **Governança:** [#356](https://github.com/escossio/attention-router/issues/356) · **PR de prova:** [#355](https://github.com/escossio/attention-router/pull/355) · **Contratos:** [#344](https://github.com/escossio/attention-router/pull/344)
**Estado:** COLETA DOCUMENTAL CONCLUÍDA PARA ESTE RECORTE; janela maior permanece ABERTA.
**Data:** 2026-10-15 · **Método:** leitura somente do GitHub App/API; nenhum comando operacional ou teste novo.

## 1. Regra de referência e errata

O SHA exato e confirmado da ponta da branch do PR #355 é **`ac67ec00a4145f3b35a56576523b27ded5044d62`** (40 caracteres). Um SHA com um `f` adicional foi transcrito incorretamente nos primeiros registros da janela; aquele valor de 41 caracteres **não** deve ser usado para consultar o GitHub. O erro foi registrado com correção aditiva na issue #356, sem reescrever o histórico.

## 2. Evidência da execução CI já realizada

| Check do SHA acima | Resultado | Worker | Evidência disponível |
| --- | --- | --- | --- |
| `distributed-postgres` | SUCCESS | CI03 | 475 testes, 58 arquivos, 226 s de worker / 228 s wall; `Automatic reprofile: no` |
| `distributed-python` | SUCCESS | CI01 | 356 s de worker / 359 s wall |
| `distributed-docker` | SUCCESS | CI02 | 158 s de worker / 161 s wall |
| `distributed-transport` (shadow, não required) | FAILURE | CI02 | TypeError `packed.files` em `sdks/typescript/tests/installed-package.mjs:20`, Node 22.23.3; CI01 teve `WORKER_BUSY` e não executou esse SHA |

Os resultados se referem exclusivamente a esse commit; não implicam integração à `main`, nem certificam um banco real. O caso de transport é preservado na [issue #345](https://github.com/escossio/attention-router/issues/345) e **não** demonstra ainda uma causa específica do CI02.

## 3. Cadeia de custódia do código inspecionado

| Arquivo | Git blob SHA |
| --- | --- |
| `attention_router/investigator/disposable_pg_proof.py` | `1e8a116d90424859905c519ba2721abaf31f0eab` |
| `attention_router/investigator/disposable_stage.py` | `fc08655a33d61de0f9cdf96c7749de0825eed033` |
| `attention_router/investigator/signed_admission.py` | `656f27526a9471f0850d064e146d15616846eb24` |
| `tests/integration/test_postgres_investigator_durable_admission_v0.py` | `eaa7ac5ee5c7b952406d0e0a104f524b81f37c0a` |

## 4. Matriz: implementação, prova e limites

| Invariante | Situação no PR #355 | Evidência/limite |
| --- | --- | --- |
| Origem/tenant sintéticos admitidos | IMPLEMENTADO E TESTADO NO LABORATÓRIO | HMAC verificado pelo `SyntheticAdmission`, com binding local. Não equivale a Zabbix real nem autoridade de produção |
| Seed, nonce e audit inicial atômicos | IMPLEMENTADO E TESTADO NO DB DESCARTÁVEL | Uma transação, bytes/hash guardados, rollback com falha de auditoria. Não comprova fsync ou restart físico |
| Replay entre instâncias | IMPLEMENTADO E TESTADO NA FIXTURE | Restrição PostgreSQL `(principal_id, nonce)`; porém nonce da primeira etapa é consumido em memória antes do commit |
| Retry/conflito por identidade | IMPLEMENTADO E TESTADO NA FIXTURE | Unicidade principal/evento, comparação de tenant/source/bytes/hash; lifecycle de sessão real ausente |
| Concorrência na admissão | TESTADA | Há testes de corrida para duas admissões e mesmo nonce, dentro do DB temporário |
| Isolamento por tenant | TESTADO EM QUERIES APLICATIVAS | Filtro `seed_id + tenant_id`; não demonstra RLS, roles/grants isolados ou identidade real |
| Auditoria inicial e códigos | IMPLEMENTADO PARCIALMENTE | Grava `SEED_ADMITTED`/`IDEMPOTENT_RETRY`, aceita códigos/fingerprints; ainda não liga toda consulta/evidência/hipótese/conclusão ao storage |
| Seed persistido antes do LLM | PROVA DE STAGING | O gateway somente armazena e retorna receipt; não aciona um LLM, não prova recuperação do fluxo nem ACK operacional |
| Recuperação após crash físico, backup, restauração | NÃO TESTADO | CI PostgreSQL descartável pode usar parâmetros de durabilidade reduzidos |
| Contrato de prod/AGT | NÃO IMPLEMENTADO/AUTORIZADO | Sem role, migração ou conector real; nome permitido de banco de teste é proteção adicional, não atestado da máquina |

**Risco arquitetural a revisar:** `stage_verified` é uma função de baixo nível que recebe `principal_id` e `SourceEvent`; presume que o chamador verificou HMAC. Chamada direta fora do gateway de admissão poderia ignorar essa fronteira, caso fosse exposta em uma integração futura. Isto é **risco de desenho**, não relato de exploração.

**Risco de retry conhecido:** `SyntheticAdmission.admit` reserva nonce in-memory antes de `stage_verified` confirmar a transação. Falha entre esses passos pode recusar um retry legítimo na mesma instância. O ledger PostgreSQL presente nesta prova não resolve sozinho a ordem e o handshake do fluxo completo.

## 5. Grau de certeza da coleta

- **CONFIRMADO:** os quatro check-runs e suas conclusões no SHA indicado; existência dos arquivos e testes inspecionados.
- **TESTADO NO LABORATÓRIO:** atomicidade, replay, idempotência, concorrência e isolamento lógico do store de prova conforme casos versionados; relatório agregado da CI não apresenta um log individual por cada nome de teste.
- **AINDA NÃO DEMONSTRADO:** todos os eventos de ferramenta gravados duravelmente, imutabilidade contra administradores, autenticação de origem real, recuperação após falha física, orçamento SDK em execução e capacidade autônoma de investigar causas.

## 6. Decisão do gate

**NO-GO para produção, AGT, modelo externo ou acesso operacional.**

**GO apenas para continuar a coleta documental controlada**, ou para planejar uma próxima experiência sintética com pré-registro de cada comando, SHA, worker, escopo, impacto, risco e critério de parada. Não disparar teste pesado sem autorização e ficha específica.

Não corrigir a falha TypeScript, não trocar worker nem mascarar check. Preservar a issue #345 até comparação controlada do mesmo SHA entre máquinas.

## 7. Próxima coleta proposta, ainda não executada

Definir teste de falha **entre HMAC e commit** e plano de persistência completa da trilha de auditoria (queries, evidência, hipóteses e conclusão), sempre em banco descartável e apenas depois de autorização/registro de pré-execução. Nenhum comando novo foi executado nesta rodada.

**Registro de execução:** COL-20261015-006/008/009/010/011, com errata aditiva na [issue #356](https://github.com/escossio/attention-router/issues/356).