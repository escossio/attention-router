# ADR 0022 — Recuperação autenticada, idempotência e recibo do Investigador Operacional V0

## Status

**Proposed — DIREÇÃO RECOMENDADA, NÃO APROVADA PARA IMPLEMENTAÇÃO OU IMPLANTAÇÃO.**

Data: 2026-10-15. Janela formal de coleta [#356](https://github.com/escossio/attention-router/issues/356). Marco [#339](https://github.com/escossio/attention-router/issues/339). Contrato geral [#344](https://github.com/escossio/attention-router/pull/344).

## Contexto factual e cadeia de evidência

1. A prova sintética de HMAC e o store PostgreSQL do Investigador ([#353](https://github.com/escossio/attention-router/pull/353), [#355](https://github.com/escossio/attention-router/pull/355)) armazenam seed/nonce/auditoria inicial numa transação, mas `SyntheticAdmission.admit` consome `_seen.add` em memória **antes** de `DisposableSignedStage.stage_signed` chegar à transação.
2. O teste F01 isolado ([#357](https://github.com/escossio/attention-router/pull/357), SHA `b6fc9e1f26be53cfb25e473c2d3e78613e5047c1`) reproduziu **na fixture** o estado: nenhum seed/nonce/audit no PostgreSQL e nonce já consumido na mesma instância. Evidência nominal `1 PASS / 1 XFAIL` com JUnit SHA-256 `197e04ba9ebf2e4ebdc1da5f7538853c2681208eea9e53aea272def1aad92f0e`. [Relatório F01](../operations/investigator-v0-collection-report-f01-executed.md).
3. A XFAIL representa proposta de reaceitar o mesmo envelope/nonce; **não é requisito de segurança ratificado**. Proteção antirreplay pode justificar negar esse envelope. A necessidade real é recuperar o evento/recibo sem sessão duplicada.
4. **Código já existente na `main`** implementa admissão de integração com credencial/binding, tenant server-owned, `integration_inbox` durável, comparação de bytes brutos e recibo recuperável em retry autorizado. Fontes: [ADR 0002](0002-inbound-idempotency.md), [ADR 0020](0020-neutral-ingress-and-authenticated-tenant-binding.md) e `attention_router/integrations/admission.py` no SHA da main `078dd7425c839f90265428a010d30a90f66090d1`. Os testes `tests/integration/test_postgres_integration_admission.py` incluem falha de commit/rollback, reenvio do mesmo conteúdo, concorrência, revogação e isolamento. Essa interface é voltada a `inbound_event` de integrações; **não está provada compatível com eventos operacionais do Investigador**.

## Problema de decisão

Precisamos tratar separadamente: (a) autenticação da origem e autorização de tenant; (b) frescor/replay da requisição de transporte, quando necessário; (c) identidade idempotente do evento; (d) commit durável de bytes/recibo; (e) recuperação de ACK perdido; (f) criação de **uma única sessão lógica**, sem alegar exactly-once para ferramentas externas.

O F01 não deve ser corrigido simplesmente removendo a checagem de nonce ou aceitando qualquer reenvio. Ao mesmo tempo, não podemos deixar uma falha entre autenticação e commit perder um evento legítimo sem protocolo de recuperação.

## Alternativas examinadas

| Opção | Benefício | Risco/lacuna | Parecer |
| --- | --- | --- | --- |
| A — Reenviar com assinatura/nonce novo, mantendo event ID e bytes idênticos | Mantém antirreplay do envelope antigo e permite recuperação quando não houve commit | Sozinha não recupera ACK perdido sem lookup idempotente; cliente precisa mecanismo legítimo de nova assinatura | Candidata a mecanismo de retry quando assinatura por mensagem for exigida; insuficiente sozinha |
| B — Consulta autenticada do status/recibo pela identidade do evento | Recupera commit com ACK perdido sem reexecutar investigação | Depende de vínculo de principal/tenant e identidade estável; não resolve entrega inicial perdida se fonte não puder refazer solicitação | Necessária como capacidade contratual, mesmo se exposta inicialmente por retry idempotente |
| C — Nonce/seed/audit em transação PostgreSQL sem `_seen` como autoridade | Retira split de estado F01 entre RAM e DB e dá unicidade cross-instance | Ainda precisa política de retry/ACK, retenção TTL, transação e tratamento de falha ambígua | Obrigatória apenas se adotarmos nonce de mensagem para essa origem |
| D — Reutilizar semântica e, quando possível, componentes da admissão neutra existente (ADR 0020) | Evita segundo motor de idempotência; binding/tenant/recibo/rollback já têm testes maduros | O schema V1 `inbound_event` e o contrato de integrações **não** são equivalentes a alertas operacionais; exige adapter e testes de isolamento | **Recomendação para revisão**, não decisão implementada |

## Direção recomendada para apreciação

**Preferir D como fronteira canônica**, complementada pela semântica B de recuperação autenticada. Antes de promover, demonstrar em branch/testes sintéticos que o Investigador consegue usar uma **admissão idempotente vinculada a principal/tenant** sem duplicar o registry, enfraquecer scopes ou alterar comportamentos existentes do ingress. Não usar o endpoint V1 com payload incompatível por conveniência.

### Contrato-alvo (proposto)

1. A origem é autenticada e vinculada pelo servidor a **principal, tenant, integração/instância e escopos**, antes de confiar no evento. O payload não seleciona tenant ou privilégios.
2. Identidade estável por binding + evento (`external_event_id` e/ou `idempotency_key`), com verificação da igualdade **byte a byte** dos bytes que compõem o registro recebido. Conteúdo divergente sob mesma identidade gera `IDEMPOTENCY_CONFLICT`, sem recibo novo.
3. Receber evento significa gravar bytes originais relevantes, hash, proveniência, binding e trabalho pendente numa **única fronteira de commit**. ACK `ACCEPTED` só após commit; ACK não significa investigação concluída ou operação aprovada.
4. Requisição idêntica sob credencial **atualmente válida** retorna `DUPLICATE` e o mesmo recibo da entrada persistida; revogação ou vínculo inválido precede diagnóstico privado e impede recuperação por credencial não autorizada.
5. A criação posterior da sessão do Investigador deve referenciar recibo/seed imutável e ser idempotente, sob chave/constraint independente. **Não** criar duas sessões ou autorizar duas consultas porque a entrega do ACK ocorreu duas vezes.
6. Em falha confirmada antes do commit, negar ACK e permitir **retry autenticado**. Em falha ambígua entre commit e resposta, consultar de modo autenticado a identidade/recibo existente antes de iniciar novo trabalho; nunca atribuir `NOT_COMMITTED` sem evidência do estado real.
7. Se houver requisito comprovado de HMAC por mensagem (não estabelecido por ADR 0020 para o novo ingress), o mesmo envelope/nonce pode permanecer bloqueado. Recuperação se dá por **nova assinatura/nonce** com identidade do evento estável ou consulta autenticada de recibo; o claim de nonce precisa ser transacional/durável e separado de semântica de evento.
8. A política de retenção do identificador e nonce precisa ultrapassar o horizonte real de retry; expiração não pode reabilitar execução duplicada ainda pendente. Taxas de consulta e enumeração de recibos são limitadas por principal/tenant.
9. Conclusão causal, status de ferramenta, aprovações e gastos do LLM são objetos separados do recibo de ingress. Recibo de entrada não garante exactly-once de efeitos externos nem resultado de investigação.

### Estados lógicos para testes (ainda não implementados)

| Condição | Resposta interna/externa permitida | Efeito sobre a investigação |
| --- | --- | --- |
| Primeiro evento autenticado e commit confirmado | `ACCEPTED` + recibo | Trabalho pendente durável; sessão ainda não necessariamente iniciada |
| Reenvio idêntico autorizado após commit | `DUPLICATE` + **mesmo** recibo | Não iniciar nova sessão |
| Mesmo event ID com bytes divergentes | `IDEMPOTENCY_CONFLICT` | Não alterar primeiro seed/recibo |
| Falha PostgreSQL antes do commit | `INGRESS_UNAVAILABLE`, sem afirmar receipt | Sem sessão nem admissão parcial; retry autorizado posteriormente |
| Commit ocorrido mas resposta perdida | Estado inicialmente **DESCONHECIDO** no cliente; retry ou consulta autenticada recupera recibo | Não produzir outro seed/sessão |
| Binding revogado/inválido | `UNAUTHENTICATED` ou `BINDING_FORBIDDEN` | Não revelar status ou tenant de terceiros |
| Assinatura repetida e nonce exigido pela origem | Bloquear *replay de transporte* sem bloquear recuperação legítima por nova credencial/frescor | Nenhum segundo efeito do mesmo evento |

Os códigos acima vêm da família de contratos de admissão existente e são exemplos de **semântica a verificar**; não afirmam que o Investigador já exponha esses endpoints/códigos.

## Relação com contratos existentes — pontos de não equivalência

- ADR 0020 usa credencial Bearer com TLS para binding; o HMAC da prova #353 é **sintético e independente**. Não transformar `SyntheticBinding.key` em credencial de produção nem presumir assinatura Zabbix sem verificar interface real.
- `IntegrationInboxRow` persiste `raw_body` do V1 `inbound_event`, não necessariamente os bytes do alerta Zabbix sob o formato atual. Precisamos especificar qual é o original imutável do Investigador, qual seu hash e em qual transação se torna durável.
- O contrato de auditoria atual do Investigador contém somente parte da trilha; reutilizar ingress não persiste automaticamente invocações, evidências, hipóteses e conclusão.
- O status `PENDING` do inbox é estado de recepção, não status `RUNNING/CONCLUDED` do Investigador; transições e consumo idempotente requerem mecanismo separado e contrato de recuperação.
- Código/testes da main comprovam certas propriedades no contexto de **integrações**; a adaptação para investigação não herda essa certificação automaticamente.

## Experimentos e critérios de adoção (todos pendentes)

- [ ] E01 — teste em PostgreSQL descartável de ingress compatível com evento operacional, sem alterar famílias V1 não relacionadas; confirmar original e hash preservados.
- [ ] E02 — falha pré-commit seguida de retry autenticado; zero seed/nonce/audit parcial e receipt criado uma vez após recuperação. Incluir comparação F01, não ajustar resultado histórico.
- [ ] E03 — commit confirmado com ACK perdido; recuperar exatamente o mesmo recibo sem segunda sessão, inclusive em outra instância.
- [ ] E04 — duas requisições concorrentes com mesma identidade e bytes idênticos, e duas com bytes conflitantes; verificar unicidade, resultado e isolamento de tenant.
- [ ] E05 — revogação/rotação de credencial antes e depois de admit; impedir acesso ao recibo por principal sem vínculo ativo.
- [ ] E06 — se houver HMAC por mensagem, demonstrar replay de nonce negado e retry com nonce novo/assinatura nova associado ao mesmo evento; ledger DB durável e TTL compatível com retenção.
- [ ] E07 — início idempotente de sessão a partir de um recibo, crash entre claim e criação da sessão e recuperação sem repetir ferramentas.
- [ ] E08 — teste de autorização/roles/RLS para armazenamento e auditoria de consultas do Investigador; sem leitura cross-tenant.
- [ ] E09 — relatório nominal, logs sem segredo, budget e saída NO-GO caso evidência falhe, seguindo a janela #356.

Não disparar esses experimentos a partir desta ADR. Cada um exige ficha prévia, orçamento, comando exato, SHA, worker, critério de parada e autorização dentro da janela.

## Consequências, governança e decisão

**Recomendação de desenho:** privilegiar um único mecanismo de admissão durável e identidade vinculada, com camada adaptadora específica para o Investigador, evitando uma segunda autoridade concorrente de nonces/eventos. Separar o receipt da execução/autorização de ferramentas.

**Estado efetivo:** ADR PROPOSTA. Sem aceitação humana, sem alteração de `main`, sem migração, sem mudança na admission existente, sem decisão de habilitar HMAC real, modelo, Zabbix, AGT ou implantação.

**Critério mínimo de aprovação:** revisão do contrato por responsável técnico; compatibilidade de escopo/tenancy com ADR 0020; prova de recuperação sob F01/F06 e controle antirreplay; estratégia de migração compatível e reversível; certificação CI e custódia de evidência na issue #356. Um futuro PR de remediação é separado da coleta e deve citar este ADR com status formal atualizado.

**Fontes primárias:** [#356](https://github.com/escossio/attention-router/issues/356), [Relatório F01](../operations/investigator-v0-collection-report-f01-executed.md), [ADR 0002](0002-inbound-idempotency.md), [ADR 0020](0020-neutral-ingress-and-authenticated-tenant-binding.md), `attention_router/integrations/admission.py` e `tests/integration/test_postgres_integration_admission.py` no SHA `078dd7425c839f90265428a010d30a90f66090d1`.