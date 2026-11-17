# Revisão técnica 001 — ADR 0022 / Investigador Operacional V0

**Data:** 2026-10-15 · **Parecer do revisor técnico (ChatGPT): `REQUEST_CHANGES`** · **Estado da ADR:** `Proposed`, sem aceitação humana. A revisão não cria permissão de merge, CI manual, deploy ou conexão operacional.
**Janela de custódia:** [#356](https://github.com/escossio/attention-router/issues/356) · **Issue de decisão:** [#358](https://github.com/escossio/attention-router/issues/358) · **ADR analisada:** [0022](../adr/0022-investigator-admission-receipt-recovery.md) · **Evidência F01:** [relatório](investigator-v0-collection-report-f01-executed.md).

## 1. Escopo e fontes congeladas

| Fonte | SHA/ref | Papel na revisão |
| --- | --- | --- |
| PR #344 (ADR 0022) | `5df14745e0e183cd5d1f99c96444e90552281e1b`, blob ADR `82adc6b91005eaf2c4d6708f222fc0f03c81b7b0` | Proposta, não implementação |
| `main` | `078dd7425c839f90265428a010d30a90f66090d1` | Contratos existentes e implementação |
| `attention_router/integrations/admission.py` | blob `688f8d2872099b2a5211a9eb1d18c63c9a2af537` | Transação de admissão/receipt |
| `attention_router/integrations/tenant_binding.py` | blob `82a47583a047d1ccb5d9d77497ec3384f7d74d0c` | Principal/credencial/binding/scope |
| `attention_router/infrastructure/models.py` | blob `8ff24d0bd2957b1ba53f0bc30c159dbbcc9617e1` | DDL implícita/constraints do inbox |
| `attention_router/web/integration_ingress.py` | blob `a1aaf4828825c5cc29540ae124c36000401de0ba` | Wire, HTTP, auth headers, switch de rollout |
| `tests/integration/test_postgres_integration_admission.py` | blob `4ee21b4d764062443c01c64c6dad4a5b2b0ec683` | Evidência de testes para integração V1 |
| #355 / #357 | `ac67ec00a4145f3b35a56576523b27ded5044d62` / `b6fc9e1f26be53cfb25e473c2d3e78613e5047c1` | Prova sintética F01, não incidente real |

**Limite:** revisão estática de GitHub e dados já coletados. Nenhum teste ou host foi acessado nesta revisão; a implementação do Investigador continua em branches draft.

## 2. Parecer resumido

**Aprovar a direção de evitar duas autoridades concorrentes de identidade e idempotência; não aprovar ainda a ADR como contrato executável.** O padrão de credencial vinculada a tenant e recibo transacional do Attention Router deve orientar um adaptador de evento operacional. No entanto, o endpoint e a tabela V1 não podem ser reutilizados automaticamente, pois impõem tipos, limites e semânticas diferentes.

## 3. Achados bloqueantes

### ADR22-R1 — incompatibilidade de tipo e contrato (BLOQUEANTE)

`IntegrationInboxRow` possui `CheckConstraint("contract_type = 'inbound_event'")`; a rota neutra aceita `application/json` V1 `inbound_event`, não o payload operacional do Investigador. Os `kind` atualmente aceitos por binding são `CHANNEL` ou `CAPABILITY`. Adaptar eventos operacionais exigirá definir tipo/família e adapter, validar sem perda de informação, e decidir se a persistência usará extensão segregada/revisionada ou store próprio que reutilize primitivas confiáveis. **Não** converter alerta em mensagem falsa, inventar actor/content nem acrescentar um novo `contract_type` sem decisão/DDL e regressão.

### ADR22-R2 — novo escopo/autoridade e audiência (BLOQUEANTE)

A permissão existente é `integration:inbound_event:write`; ela não concede licença para ler dados operacionais, iniciar investigação ou alterar infraestrutura. Credencial autenticada precisa resolver `principal→tenant→source/resource→capabilities` fora do payload e fora do modelo. Contrato novo deve preservar revogação, rotação, active tenant, ambiente/audience, tenant isolation e negar misturas de headers de autoridade. Ingress V1 atualmente rejeita headers HMAC alternativos misturados com Bearer. **Não** usar o endpoint existente para superar isso.

### ADR22-R3 — bytes imutáveis e semântica de nonce (BLOQUEANTE)

Na implementação existente, o replay válido retorna o mesmo recibo **somente se o corpo inteiro da requisição for byte a byte idêntico**. Se uma nova assinatura/nonce for introduzida **dentro** de `raw_body`, o retry com mesmo event ID produzirá conflito. Se assinatura de mensagem for necessária após análise da origem, definir quais bytes são o `original` imutável e separar dados voláteis de transporte com vinculação criptográfica explícita, sem trocar bytes silenciosamente nem normalizar JSON. Nonce por mensagem não está exigido pela ADR 0020; não herdar HMAC sintético como autenticação real.

### ADR22-R4 — recibo/status de recuperação (BLOQUEANTE)

ADR 0020 e `admit_inbound` já permitem recuperar recibo pelo **reenvio idêntico autenticado** após perda de ACK (`DUPLICATE` e mesmo ID). O perfil inicial **não tem operação de status/polling**. A ADR 0022 deve distinguir o que existe do que é proposto: (i) retry idêntico com binding ativo, já implementado para integração V1; (ii) eventual consulta de recibo por identidade autenticada, contrato novo com autorização/anti-enumeração, ainda não implementado. Credencial revogada não recupera recibo por esse caminho.

### ADR22-R5 — identidade da sessão e auditoria integral (BLOQUEANTE)

Recibo de recebimento (`integration_inbox.state=PENDING`) não é sessão, investigação concluída nem autorização de ferramentas. Necessário contrato separado `receipt_id→investigation_id` com claim idempotente, lease, recuperação após crash, evidência e auditoria por consulta, estado terminal e segregação por tenant. Não presumir exactly-once de efeitos externos pelo fato de entrada ser idempotente.

### ADR22-R6 — limites do baseline experimental (BLOQUEANTE PARA PRODUÇÃO)

F01 demonstrou em fixture a janela `_seen` in-memory antes do commit, com 1 PASS observacional e 1 XFAIL de tentativa de retry do mesmo nonce. A XFAIL **não prova** que reaceitar o nonce seja solução segura. Os testes V1 existentes comprovam seu próprio contrato PostgreSQL, não compatibilidade com Zabbix ou o Investigador. Nenhum endpoint real, migração, RLS, backup/restore ou modelo está certificado.

## 4. O que reutilizar e o que NÃO reutilizar

| Reutilização candidata | Parecer | Exigência |
| --- | --- | --- |
| Registry confiável de principal/tenant/binding, checks de lifecycle e scopes | REUTILIZAR COMO CONTRATO/PRIMITIVA | Auditar autoridade e nomenclatura operacional, evitar scope implícito |
| Transação de inbox e constraints de unicidade por binding/event ID | REUTILIZAR SEMÂNTICA; CÓDIGO SOMENTE APÓS ESTUDO | Definir identidade de evento operacional, schema, índice, migração e regressão V1 |
| Receipt imutável e retry byte-identical | REUTILIZAR SEMÂNTICA | Separar original do envelope de frescor; testar commit perdido/ACK perdido |
| Endpoint V1 `/api/v1/ingress/integrations/events` sem alteração | NÃO REUTILIZAR DIRETAMENTE PARA ALERTA NÃO COMPATÍVEL | Novo contrato/adapter aprovado ou compatibilidade formal demonstrada |
| HMAC/nonce da fixture `SyntheticAdmission` | NÃO PROMOVER AUTOMATICAMENTE | Modelo de ameaça e interface real da origem; ledger durável se necessário |
| Leitura de Zabbix/Tempo/CI e ciclo de ferramentas | NÃO CONCEDIDA PELA ADMISSÃO | Grants somente-leitura independentes, orçamento e auditoria |

## 5. Revisões obrigatórias da ADR 0022

- [ ] Especificar interface de evento operacional e quais bytes originais são recebidos/conservados; declarar relação entre identidade de origem, `external_event_id`, `idempotency_key`, digest e payload de transporte.
- [ ] Escolher abordagem para reuse: abstração compartilhada de admission/tenant/receipt com adapter segregado **versus** extensão versionada do V1 com compatibilidade demonstrada. Proibir duas autoridades diferentes para o mesmo principal/tenant.
- [ ] Atribuir scopes distintos para ingestão operacional, consulta de recibo e início de investigação; revogação/tenant devem ser testados antes de lookup.
- [ ] Diferenciar retry autenticado de corpo imutável (existente) de status-query (nova); escolher se status-query é mesmo necessária no V0.
- [ ] Decidir se HMAC por mensagem é exigido conforme origem real. Se sim, onde ficam nonce, assinatura, timestamp e ledger durável sem conflito de raw-body equality.
- [ ] Definir semântica de receipt após ACK perdido, falha pré-commit e perda de conexão com banco com outcome desconhecido; nunca tratar 503 como prova de ausência de commit.
- [ ] Definir identidade/idempotência de sessão e audit trail por invocation/evidence/model outcome, além de rollback e retenção.
- [ ] Definir isolamento de base/roles, event/receipt tombstones e escopo de recurso operacional, sem ativar produção.
- [ ] Manter F01 histórico congelado e criar testes de contrato em branch isolada somente após autorização da #356.

## 6. Prioridade dos experimentos (não autorizados por este relatório)

**E00 — compatibilidade semântica e autoridade (primeiro):** em fixture, um evento operacional completo precisa conservar bytes, source, tenant e ID próprios sem fingir que é mensagem V1. Verificar que credencial `integration:inbound_event:write` sozinha **não** permite ação de investigador.

**E01/E02 — persistência inicial e falha pré-commit:** seed e recibo atômicos no DB descartável; erro sem recibo seguido de retry autorizado e sem sessão duplicada.

**E03 — ACK perdido depois do commit:** reenvio idêntico com credencial válida retorna mesmo receipt; teste opcional separado para status lookup **caso seja contratado**.

**E04/E05 — concorrência, conflito, revogação e rotação:** mesmo binding, evento/bytes e credencial renovada; não atravessar tenant e não entregar recibo a principal revogado.

**E06 — se houver HMAC por mensagem:** nonce transacional, assinatura por origem e retry de transporte não conflitando com evento imutável.

**E07/E08/E09 — sessão única, auditoria, roles, custo e coleta nominal:** demonstrar não executar ferramentas em duplicidade após retry/worker restart e não vazar dados de tenants.

Cada experimento exige ficha prévia na #356 com SHA, worker, ambiente descartável, orçamento, critérios PASS/FAIL/INCONCLUSIVE e evidência em relatório sanitizado. **Nenhum teste novo/merges foi autorizado por este parecer.**

## 7. Decisão formal do revisor

**`REQUEST_CHANGES`** à redação/contrato da ADR 0022. **A direção D é recomendada para amadurecimento**, não rejeitada. A revisão não substitui aceite humano: ADR permanece `Proposed` e issue #358 aberta até critérios resolvidos e revisor autorizado registrar `ACCEPT` ou outro parecer. Os PRs do Investigador e a prova F01 permanecem em draft; não há permissão operacional, deployment, HMAC real ou modelo ativo.

**Cadeia de custódia:** COL-20261015-031 (análise e observações), COL-20261015-032 (publicação deste relatório) na [janela #356](https://github.com/escossio/attention-router/issues/356).