# E00 — Matriz de compatibilidade de entrada e de autoridade do Investigador V0

**Janela:** [#356](https://github.com/escossio/attention-router/issues/356) · **Revisão:** [#358](https://github.com/escossio/attention-router/issues/358) · **ADR:** [0022 Proposed](../adr/0022-investigator-admission-receipt-recovery.md) · **PR de prova negativa:** [#359](https://github.com/escossio/attention-router/pull/359).
**Estado ao elaborar:** TESTES VERSIONADOS, CERTIFICAÇÃO CI PENDENTE. Não é aceitação de ADR nem prova de integração operacional.

## 1. Proveniência

| Objeto | SHA / blob | Observação |
| --- | --- | --- |
| `main` / admissão V1 | `078dd7425c839f90265428a010d30a90f66090d1` | Contrato aprovado de integrações |
| `attention_router/integrations/tenant_binding.py` | blob `82a47583a047d1ccb5d9d77497ec3384f7d74d0c` | `INBOUND_SCOPE`, credencial, binding |
| `attention_router/integrations/admission.py` | blob `688f8d2872099b2a5211a9eb1d18c63c9a2af537` | Parser V1, transação e recibo |
| `attention_router/infrastructure/models.py` | blob `8ff24d0bd2957b1ba53f0bc30c159dbbcc9617e1` | `integration_inbox` e CHECK/uniqueness |
| Base do Investigador PR #355 | `ac67ec00a4145f3b35a56576523b27ded5044d62` | `SourceEvent` e `Investigator` sem adaptação operacional |
| PR #359 E00 | `bf0415fba3fd6d3959ab4f6cd0a51cb504aa7cc5` | 1 arquivo novo de teste, 186 inserções, 0 remoções |

## 2. O que a prova realmente verifica

| Caso | Verificação proposta no arquivo de testes | O que demonstra se passar | Não demonstra |
| --- | --- | --- | --- |
| E00-01 | `SourceEvent.original` preserva bytes opacos e `digest` calcula SHA-256 dos mesmos bytes | Dados do alerta podem ser carregados em DTO sem reserializar | Autenticidade, autorização, retenção durável |
| E00-02 | Parser `inbound_event` V1 rejeita `investigator_event` | Contrato antigo não admite silenciosamente uma família nova | Novo contrato operacional correto |
| E00-03 | Binding V1 rejeita família operacional mesmo com credencial de entrada válida | Credencial V1 não faz integração virar Investigador | Regra de credencial operacional futura |
| E00-04 | Tenant ou instância de origem divergente no body são rejeitados | Claims não substituem a identidade de binding | Isolamento real em banco/roles da entidade |
| E00-05 | Um scope operacional apenas proposto não substitui `integration:inbound_event:write` | Ausência de autorização implícita entre superfícies | Nome, semântica ou concessão real do novo scope |
| E00-06 | `Investigator` não infere tenant/grants de credencial V1; nega consultas sem allowlist explícita | Dois limites independentes: entrada ≠ permissão de ferramenta | Broker/admission adapter real ou storage durável |

**Declaração:** os seis casos são testes sintéticos NEGATIVOS / de fronteira. Nenhuma linha de produção, integração operacional, Zabbix ou banco é alterada. PASS da CI só valida asserções existentes, não demonstra a arquitetura final.

## 3. Compatibilidade positiva ainda ausente

1. **Identidade do evento:** definir `source_event_id` estável, original opaco, digest e vinculação principal/tenant/ambiente sem manipulação de bytes.
2. **Contrato de origem:** decidir como autenticar um alerta operacional, incluindo source/resource autorizados, revogação e scope de ingresso diferente de `integration:inbound_event:write`.
3. **Framing e representação:** não inserir `investigator_event` no CHECK `contract_type='inbound_event'` sem decisão. Não apresentar alerta como `event_type='message'` só para satisfazer o V1.
4. **Recibo e recuperação:** separar bytes do evento original dos metadados voláteis de transporte (timestamp/nonce) quando aplicável, sem conflito de identidade idempotente; ACK somente após commit.
5. **Sessão única:** receipt / seed persistido não concede início de ferramentas; `receipt_id` deve mapear uma sessão do Investigador sob autorização, com claim e audit separadas.
6. **Governança:** manter F01 histórico congelado. Testes do adaptador positivo exigem proposta de schema, revisão da ADR 0022 e nova ficha de autorização na #356, sem abertura de endpoint real.

## 4. Gates

- [ ] Verificar checks Python e PostgreSQL do SHA exato do PR #359. Transport/Docker shadow devem ser observados, não tratados como autoridade para o contrato sem revisão.
- [ ] Conferir se o novo PR altera **somente testes**, sem código de runtime/ingress.
- [ ] Registrar resultado nominal dos testes E00, ou explicar limitação se a CI só fornecer resumo agregado.
- [ ] Identificar o formato aprovado do alerta operacional e o vínculo de tenant/resource fora do payload.
- [ ] Avaliar escolha de schema/adapter isolado e estratégia de reuso de primitivas em contrato revisado da ADR #358.
- [ ] Não iniciar E01/E02 ou correção F01 sem pré-registro e autorização por comando.

**Estado GO/NO-GO:** GO somente para revisão E00 offline; **NO-GO para adaptar ingress real, aceitar ADR 0022, chamar modelos ou implantar no AGT**.