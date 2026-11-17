# Plano de Coleta 002 — Janela HMAC → commit PostgreSQL

**Projeto:** Investigador Operacional V0 · **Janela:** `INV-V0-COLLECT-20261015` · **Issue de controle:** [#356](https://github.com/escossio/attention-router/issues/356)
**Estado:** PLANEJADO, NÃO EXECUTADO. Este documento **não** autoriza implementar, injetar falhas, executar CI ou alterar infraestrutura.
**Fonte de código examinada:** [PR #355](https://github.com/escossio/attention-router/pull/355), SHA exato `ac67ec00a4145f3b35a56576523b27ded5044d62` (40 hex), mantido em draft.
**Coleta anterior:** [Relatório 001](investigator-v0-collection-report-001.md), registros `COL-20261015-012/013` na #356.

## 1. Pergunta central

Em que instante o sistema considera um evento **admitido**, em que instante o considera **registrado duravelmente** e o que acontece se houver falha entre essas decisões? São estados diferentes e não podem ser tratados como sinônimos.

### Sequência comprovada por leitura estática

1. `SyntheticAdmission.admit` verifica envelope/HMAC e acrescenta `(principal_id, nonce)` a `_seen` in-memory.
2. `DisposableSignedStage.stage_signed` somente depois chama `DisposablePgSeedStore.stage_verified`.
3. `stage_verified` abre transação SQL, grava ou compara seed/hash, tenta inserir nonce com unique constraint, grava auditoria inicial e realiza commit ao sair do contexto transacional.
4. `DurableStageReceipt.may_start_new_investigation` é `true` somente quando o seed é criado. Nenhum modelo é chamado por este gateway.

**Gap AR-INV-GAP-001, confirmado no fluxo:** `_seen` não faz parte da transação. Rollback no PostgreSQL não desfaz nonce consumido na instância.
**Gap AR-INV-GAP-002, ainda sem fault injection:** após commit com resposta perdida, cliente pode ficar sem recibo; retry do mesmo nonce atualmente colide com as proteções de replay. Uma nova assinatura com outro nonce pode reusar seed, mas não é substituto de um protocolo de recuperação aprovado.
**Gap AR-INV-GAP-003:** o armazenamento de seed e auditoria inicial não persiste a trilha completa de invocações, evidências, revisões de hipóteses e conclusão do Investigador.
**Gap AR-INV-GAP-004:** `stage_verified` é uma API interna que depende de caller autenticado; não deve ser exposta como autoridade externa.

## 2. Invariantes sob teste

| ID | Invariante | Critério de aceitação futuro |
| --- | --- | --- |
| INV-AUTH | Tenant e origem derivam de binding autenticado, nunca de texto do evento | qualquer adulteração falha antes de iniciar sessão |
| INV-ATOMIC | Seed, nonce e audit inicial são uma unidade de commit | nada parcial fica visível após rollback |
| INV-REPLAY | Um nonce já aceito não cria segunda investigação | concorrência e retry não violam unicidade |
| INV-RETRY | Falha sem commit não faz perder definitivamente um evento legítimo | procedimento de retry aprovado recupera/repete com idempotência |
| INV-ACK | Commit confirmado com resposta perdida é recuperável sem duplicação | recibo recuperável por identidade autorizada, sem tratar replay como nova admissão |
| INV-TENANT | Recuperação/consulta/auditoria sempre obedecem tenant autenticado | impossibilidade de leitura/escrita cruzada, inclusive com IDs conhecidos |
| INV-AUDIT | Todas as decisões de negócio são auditáveis com proveniência | intenção, falha e decisão não desaparecem nem vazam payload |
| INV-SECURE | Erros/prints/checks não contêm bytes, chaves ou contexto privado | códigos estáveis, logs públicos mínimos |

## 3. Experimentos propostos — matriz de pontos de falha

A tabela é uma **previsão do comportamento atual a verificar**, não resultado observado. Cada cenário requer ficha própria `COL-...` com justificativa, comando/runner/worker/SHA, escopo, timeout, orçamento e critério de parada ANTES de execução.

| Caso | Injeção/condição simulada | Estado a observar | Hipótese e classificação atual |
| --- | --- | --- | --- |
| F00 | HMAC inválido antes de `_seen.add` | nenhuma linha de seed/nonce/audit; ferramenta não chamada | recusa correta — parcialmente coberta por teste existente |
| F01 | Exceção imediatamente após HMAC aceitar e antes de `stage_verified` | `_seen` consumido, DB vazio, retry mesmo envelope/instância recusado | **gap previsto por análise estática; NÃO TESTADO** |
| F02 | Banco indisponível ao entrar na transação, após HMAC | DB sem seed/nonce/audit, `_seen` consumido; registrar tipo de erro opaco | **gap previsto; NÃO TESTADO** |
| F03 | Erro após inserir seed e antes de inserir nonce (dentro de transação) | rollback total do DB; `_seen` continua consumido | atomicidade DB provável, retry de instância não comprovado |
| F04 | Erro após claim de nonce e antes da auditoria | rollback de seed+nonce; nenhuma sessão iniciada; `_seen` consumido | transação DB deve recuar; NÃO TESTADO nessa posição exata |
| F05 | Erro após INSERT de audit inicial, antes do COMMIT | rollback do seed/nonce/audit, sem recibo | NÃO TESTADO nesse ponto exato |
| F06 | COMMIT concluído, mas resposta/ACK descartado | DB contém exatamente um seed/nonce/audit, operador não recebe recibo | **lacuna de recuperação; NÃO TESTADO** |
| F07 | Mesmo nonce apresentado simultaneamente por duas instâncias | um seed+nonce+audit; uma decisão de replay | concorrência parcialmente coberta por teste existente |
| F08 | Mesmo evento+bytes com dois nonces válidos distintos | um seed, dois nonces, audit de retry; apenas criação inicial autoriza novo trabalho | caso coberto; repetir só com ficha específica |
| F09 | Mesmo principal/evento com bytes ou tenant divergente | rejeição, rollback, nenhum seed modificado | testes existentes cobrem variantes sintéticas |
| F10 | Falha do auditor após query autorizada | observação anterior preservada e invocação incompleta registrável | **NÃO IMPLEMENTADO na trilha durável atual** |

### Evidência mínima para cada caso

- Pré-registro: `case_id`, versão do contrato, SHA 40 hex, blob hash, máquina/worker autorizado, ambiente descartável, chaves sintéticas, estado inicial, comando exato, limite de tempo/carga, risco e motivo do teste.
- Pós-execução: instante, `run_id`, SHA realmente executado, worker, `exit_code`, snapshots sanitizados das contagens de seed/nonce/audit, resultado para retry na **mesma** e em **outra** instância, hashes comparáveis do material efetivamente coletado.
- Declarar separadamente **esperado** e **observado**. Caso de retry não recuperável não é verde porque o rollback SQL funcionou.
- Registrar falha e contraprova no GitHub sem publicar payloads, HMAC, chaves, IPs privados nem logs completos.

## 4. Protocolo de autorização do ensaio

**Fase A — já permitida:** revisão read-only de GitHub e documentação. Não alterar implementação.
**Fase B — NÃO AUTORIZADA nesta janela ainda:** escrever testes novos com fault injection no branch próprio, submetê-los a revisão, e executá-los no PostgreSQL 16 **descartável** da CI distribuída, após aprovação de orçamento, ambiente, SHA e dependências.
**Fase C — NÃO AUTORIZADA:** simular falha real do worker/serviço ou desligamento abrupto; testes de processo/host são distintos dos rollback tests e exigem autorização e janela específicas.
**Fase D — NÃO AUTORIZADA:** qualquer conexão ao AGT, PostgreSQL operacional, MikroTik, Zabbix real, ou ativação de LLM.

A CI documental que possa começar por commit no PR #344 não é uma execução manual deste ensaio. A inclusão de arquivo novo na suíte PostgreSQL poderá exigir `REPROFILE_REQUIRED`; registrar o bloqueio e nunca deslocar a suíte pesada para o AGT control plane.

## 5. Critérios de GO / NO-GO

**GO apenas para avançar ao próximo teste offline controlado** após fichas prévias aprovadas, revisão dos pontos de injeção, separação de evidências e cota de recursos. Mesmo sucesso completo não autoriza produção.

**NO-GO para consolidar autenticação/persistência operacional** enquanto qualquer um persistir: nonce consumido sem commit e sem recuperação; perda de ACK sem recuperação autenticada de recibo; bypass de identidade no store; ausência de auditoria por ferramenta; isolamento de tenant sem autoridade externa; falta de testes reais de falha, restart, retenção e rollback.

Não corrigir implementação entre amostras de coleta. Se um teste revelar falha, preservar seu resultado, abrir uma decisão/PR de remediação independente e repetir ensaio somente sob outra ficha de execução.

## 6. Plano separado de auditoria integral (não implementado)

O modelo de `investigation_audit` atual possui `seq`, `seed_id`, `tenant_id`, `event_type`, `query_fingerprint`, `created_at`. Ele NÃO oferece registro estruturado de `invocation_id`, evidência criptograficamente identificada, `source_timestamp`, modelo/runtime, decisão do autorizador, revisões de hipóteses ou motivo de conclusão. É insuficiente para certificar rastreabilidade de causa.

O próximo documento técnico de design deve especificar: gravação antecipada de intenção de consulta, resultado/falha vinculados por invocation ID, hash e referência de evidência, revisão de hipótese sem sobrescrita, terminal state, recuperação de invocações pendentes, tenant authorization e política de retenção. Não criar migrations de produção a partir de um esquema conceitual.

## 7. Fontes e limites da certeza

- Código revisado no SHA `ac67ec00a4145f3b35a56576523b27ded5044d62`: `signed_admission.py` (blob `656f27526a9471f0850d064e146d15616846eb24`), `disposable_stage.py` (`fc08655a33d61de0f9cdf96c7749de0825eed033`), `disposable_pg_proof.py` (`1e8a116d90424859905c519ba2721abaf31f0eab`).
- Testes PostgreSQL versão do blob `eaa7ac5ee5c7b952406d0e0a104f524b81f37c0a`, CI do SHA com `distributed-postgres SUCCESS` (475 testes, 58 arquivos); não é rastreamento nominal de cada cenário F00–F10.
- A leitura estática prova **ordem das chamadas e invariantes de código**. Falhas F01–F06 não foram induzidas nesta janela; consequências são hipóteses técnicas condicionais, não evidência empírica.

**Decisão atual:** NO-GO para produção, modelo real e coleta em hosts. Janela #356 permanece ABERTA para evidência documental e pré-registro de futuras experiências.