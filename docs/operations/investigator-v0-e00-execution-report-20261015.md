# Relatório E00 — execução nominal de fronteiras de contrato e autoridade

**Janela:** [#356](https://github.com/escossio/attention-router/issues/356) · **Caso:** E00 NEGATIVO · **Prova isolada:** [PR #359](https://github.com/escossio/attention-router/pull/359) · **Revisão de arquitetura:** [#358](https://github.com/escossio/attention-router/issues/358) / ADR 0022 Proposed.
**Data:** 2026-10-15 (America/Fortaleza). **Resultado:** SEIS CASOS PASS NO LABORATÓRIO, ZERO PROVA POSITIVA DO ADAPTADOR.

## 1. Referência congelada

| Item | Evidência |
| --- | --- |
| SHA exato executado | `bf0415fba3fd6d3959ab4f6cd0a51cb504aa7cc5` (PR #359 draft/sem merge) |
| Base da implementação | `ac67ec00a4145f3b35a56576523b27ded5044d62` (PR #355) |
| Diff do PR #359 | Somente arquivo `tests/test_investigator_e00_contract_isolation_v0.py`, 186 inserções, 0 remoções |
| Worker usado na coleta nominal | CI01; AGT somente proxy SSH/control plane |
| Runner | `/srv/andy-ci/bin/andy-ci-run <SHA> python`, com `PYTEST_ADDOPTS=-k test_e00_ -rA --junitxml` |
| Saída operacional | `CI_SHA` exato, `CI_STATUS=PASS`, `CI_DURATION_SECONDS=59`, `E00_RUNNER_RC=0`, `E00_WRAPPER_RC=0` |
| Arquivo de evidência no CI01 | `e00-bf0415fba3fd-20261015T213532Z-3201342.xml` em `/srv/andy-ci/results` |
| SHA-256 do JUnit | `43490423c4c319664175cabe161a8af69d5448260cc19b880271e7e52450d0a8` |
| Verificação independente do JUnit | Uma correspondência de SHA-256: `CI_E00_JUNIT_MATCHES=1`, `CI_E00_JUNIT_SHA_VALID=YES` em COL-043 |

## 2. Resultados nominais

| Teste E00 | Resultado | Garantia limitada testada |
| --- | --- | --- |
| `test_e00_operational_event_preserves_exact_opaque_original_bytes` | **PASS** | DTO preserva bytes originais e digest SHA-256; nenhum poder de autenticação derivado disso |
| `test_e00_v1_integration_parser_rejects_native_operational_alert` | **PASS** | Parser V1 recusa uma família operacional `investigator_event` |
| `test_e00_valid_v1_binding_does_not_accept_operational_contract_family` | **PASS** | Binding de `inbound_event` não valida família de Investigador |
| `test_e00_payload_tenant_and_source_are_not_authoritative` | **PASS** | Declarações de tenant/source divergentes no payload não substituem binding |
| `test_e00_proposed_operational_scope_cannot_replace_v1_inbound_permission` | **PASS** | Scope operacional apenas proposto não concede acesso de ingresso V1 |
| `test_e00_v1_binding_grants_no_implicit_investigator_tenant_or_tools` | **PASS** | Um binding de ingresso válido não preenche por si só grants de tenants nem operações da entidade investigadora |

**E00_CASE_COUNT=6.** Resultados extraídos nominalmente dos `<testcase>` do relatório JUnit produzido no worker; não inferidos do resumo agregado do GitHub.

## 3. Comparação com CI automática do PR #359

Em SHA idêntico, `distributed-python=SUCCESS` no CI01, `distributed-postgres=SUCCESS` no CI03 (475 testes, 58 arquivos) e `distributed-docker=SUCCESS` no CI02. A suíte shadow `distributed-transport=FAILURE` no CI02 com `packed.files` undefined (npm 12) é um incidente de empacotamento TypeScript independente, documentado em [#345](https://github.com/escossio/attention-router/issues/345).

**Resultado não deve ser manipulado:** três checks de CI automáticos verdes não apagam Transport shadow vermelho; o erro TypeScript não invalida automaticamente a prova nominal E00 nem representa causa do Investigador. A causa npm foi posteriormente isolada e uma correção independente está em [PR #360](https://github.com/escossio/attention-router/pull/360), sem merge.

## 4. O que o E00 NÃO certifica

- Não existe receptor real de Zabbix, Linux ou CI no Investigador nesta prova.
- Não existe ligação de credenciais operacionais reais ao tenant/resource no runtime do Investigador.
- Não se afirma que o contrato `inbound_event` V1 possa transportar um alerta operacional sem novo perfil/revisão.
- Não há recibo transacional e `receipt_id→investigation_id` operacional certificado, nem garantia de sessão única sob redelivery.
- Não se certificou auditoria persistente completa de consultas, evidências, hipóteses e conclusão, nem autorização de modelo para acesso a sistemas.
- Nenhuma configuração no AGT, CI workers, MikroTik, Postgres real ou ingress de produção foi alterada.

## 5. Decisão do gate

**E00 NEGATIVO: PASS / CERTIFICADO COM EVIDÊNCIA NOMINAL EM FIXTURE.**

**E00 POSITIVO: NÃO IMPLEMENTADO, NÃO TESTADO.** Exige especificação do envelope operacional imutável, binding externo confiável, escopo de ingresso distinto, idempotência/recuperação de recibo e sessão única. Não usar mensagens fictícias nem enfraquecer o schema existente para forçar compatibilidade.

**ADR 0022:** permanece `Proposed` com parecer técnico `REQUEST_CHANGES` na issue #358. Nenhum E01/E02, merge ou deploy é autorizado por este relatório. **NO-GO para produção/modelo/integração real**.

## 6. Cadeia de custódia

O comando completo, critérios e autorização de ensaio constam na [janela #356](https://github.com/escossio/attention-router/issues/356), sobretudo COL-039 (baseline), COL-040/041 (errata e preflight), COL-042 (comando focal e resultado) e COL-043 (digest independente). Registros são aditivos; erros não foram apagados.