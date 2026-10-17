# MAESTRO Relay — compatibilidade com a cadeia instalada no estágio (17/10/2026)

**Estado: PROPOSTA DE COMPATIBILIZAÇÃO, não deploy.** A integração é uma branch derivada do PR #333 e combinada com o código já certificado do PR #368. Nenhuma mudança no Zabbix, no relay implantado, no ingress operacional nem no PostgreSQL foi feita para produzir esta branch.

## Evidência auditada

- A main do Attention Router permanece em `0057_bootstrap_selection` como head do Alembic.
- O PR #333, ainda ABERTO e com 11 checks aprovados, introduziu `0058_group_contextual_presence` a partir de 0057; esta revisão **já consta como versão do banco de estágio**. A funcionalidade contextual possui gates próprios; não alterá-los em nome do MAESTRO.
- O PR #368, também ABERTO/DRAFT e com 11 checks aprovados, introduziu em paralelo `0058_maestro_relay_receipts` a partir de 0057. Seu CI verde é válido para a branch isolada, **não comprova upgrade seguro no banco de estágio que já está na revisão do PR #333**.
- Duas revisões independentes após 0057 não podem ser tratadas como sequência única. Não renomear a migração que já foi aplicada e não reescrever `alembic_version` do banco.
- O ingress de estágio já existente utiliza a mesma base PostgreSQL que contém `0058_group_contextual_presence`; a árvore atual de produção do ingress é mais antiga que o PR #368.
- Uma consulta somente-leitura `GET /health/live` **a partir do próprio contêiner relay** retornou HTTP 200 do ingress, pela rota Docker bridge → macvlan já existente. Isso verifica conectividade de leitura, **não** autoriza enviar credencial Bearer por HTTP nem comprova HTTPS.

## Solução candidata — UMA sequência Alembic

A branch de compatibilidade tem os dois pais Git, preserva integralmente a revisão já aplicada e desloca somente a migração do MAESTRO:

`0057_bootstrap_selection → 0058_group_contextual_presence → 0059_maestro_relay_receipts`

- `0058_group_contextual_presence` mantém seu ID e conteúdo originais.
- `0059_maestro_relay_receipts` tem `down_revision=0058_group_contextual_presence`; cria somente tabela de recibos do transporte MAESTRO, na base já existente, preservando constraints e bloqueio de downgrade destrutivo.
- Os testes que consultam `alembic_version` depois de `upgrade head` foram reconciliados estritamente para a única revisão final 0059. Foram preservadas verificações de migrações históricas e uma asserção explícita da cadeia 0057→0058→0059.
- O módulo `attention_router/maestro`, endpoint opt-in e escopo/tenant já certificados no PR #368 continuam sem execução automática, sem motor de chamados, sem prioridade, sem IA e sem despachos.
- A correção npm 10/12 de SDK é a mesma já certificada no PR #360; não desabilitar testes nem regras.

**Dependência de merge:** o PR de compatibilidade tem como base o branch do PR #333, **não a main**. Só pode ser integrado à main depois que o histórico do PR #333 estiver reconciliado. O PR #368 original não deve ser aplicado isoladamente no banco de estágio. Se outros commits introduzirem migrações posteriores, refazer esta análise de lineage em novo SHA, nunca reutilizar automaticamente o plano.

## Comunicação e autoridade — sem transição de rede neste incremento

| Iniciador | Destino | Transporte | Estado | Autoridade | Zona |
| --- | --- | --- | --- | --- | --- |
| Relay V0 | Ingress existente | HTTP GET /health/live | **Comprovado, apenas leitura** | Sem credencial, sem evento | bridge do relay → macvlan ingress existente |
| Relay V0 | Futura rota MAESTRO no ingress existente | HTTPS autenticado | **Não implantado** | Bearer com binding, tenant e escopo dedicado no ingresso | fronteira privada existente; TLS ainda a homologar |
| Ingress existente | PostgreSQL de estágio existente | SQL | **Conexão atual observada, sem migração** | credenciais já governadas | ingresso → DB estágio |

O ingress já escuta HTTP interno. Não trocar o protocolo dessa porta sem analisar seus clientes atuais. O futuro HTTPS deve ser oferecido em caminho privado que preserve a compatibilidade dos demais consumidores; preferir extensão do ingresso existente, não outro serviço permanente. **Não utilizar HTTP para o Bearer**. Não abrir portas públicas; nenhuma VLAN, rede, bridge, IP estático ou L2 dedicado é exigida por esta prova. Não conectar arbitrariamente o relay a segunda rede.

## Critérios de promoção ainda pendentes

1. Checks distribuídos no SHA **combinado** em CI; testes PostgreSQL somente nos workers com banco descartável, sem migrations no AGT.
2. Ensaio de upgrade idempotente **0058→0059** e bloqueio de downgrade com recibos, usando snapshot descartável do estado de estágio, nunca banco operacional.
3. Backup e validação do PostgreSQL do estágio antes de uma janela de alteração; não alterar `alembic_version` à mão.
4. Novo runtime do ingress derivado de SHA compatível com os atuais serviços de estágio; flag `maestro_relay_ingress_enabled=false` por padrão.
5. Endpoint HTTPS privado, certificado confiável pelo relay, identidade de origem provisionada sem copiar secrets para Git.
6. Ensaio autenticado com payload sintético **a partir do contêiner Relay real**: 202 após commit, replay 200, conflito 409, recovery e indisponibilidade/retry. Só então considerar Zabbix real.
7. Monitorar disco raiz AGT (97% ocupado) antes de novas imagens e retenção; sem limpeza não avaliada.

**Rollback:** esta branch não toca runtime. Na implantação futura, manter a flag OFF, rollback da imagem com retenção de recibos e migração não destrutiva; não reverter 0058 do grupo. A presença contextual e os alertas Zabbix atuais não devem ser interrompidos.

**Live continuity:** não alterada. **Historical acceleration:** backfill adiado; nenhum cursor live alterado ou eventos históricos importados.

## Prova incremental específica (sem tocar no banco do AGT)

A compatibilização inclui também `test_pg_upgrade_from_existing_group_0058_to_maestro_0059_keeps_group_state`, em `tests/integration/test_postgres_maestro_relay_ingress.py`. No PostgreSQL **descartável do CI**, grava um grupo inteiramente sintético com modo OBSERVE, rebaixa apenas a revisão candidata 0059 para 0058 (o teste só parte de banco sem recibos), comprova que o grupo permaneceu intacto, sobe novamente para o head 0059 e registra um recibo MAESTRO sintético. Qualquer erro impede o gate PostgreSQL. Este ensaio não significa que a DB real do estágio já foi migrada e não autoriza descartar histórico do grupo.

## Evidência do teste incremental e diagnóstico do gate — 17/10/2026

- **SHA de implementação testado:** `b4fdb944d55805a5788b66399c1c74e11128057b`. O escalonador oficial `andy-ci-distributed`, comandado no AGT (somente control plane) e executado no **worker CI03**, reportou no run `20261017T143342-b4fdb944d558-postgres-distributed`: `status=PASS`, `degraded=false`, **471 testes PASS**, **58 arquivos**, uma wave e nenhuma reexecução. O novo teste `test_pg_upgrade_from_existing_group_0058_to_maestro_0059_keeps_group_state` integra essa suíte.
- **CodeQL e Public CI** executados manualmente no mesmo SHA via workflow_dispatch também concluíram `success`; os checks Docker/Python/Transport distribuídos registraram `PASS` no SHA.
- **Distinção obrigatória:** o check PostgreSQL criado previamente pelo controle do GitHub encerrou com `failure_class=NO_DISCOVERY_CAPACITY` / `WAITING_FOR_DISCOVERY_CAPACITY` (0 testes), após aguardar o CI03 que estava ocupado. Esse status **NÃO** deve ser escondido nem confundido com os 471 testes PASS de outra execução do mesmo SHA. A API de re-request do check do aplicativo próprio retornou HTTP 404; nenhum check foi falsificado ou desativado.
- A próxima revisão altera **somente esta documentação de evidências**, sem modificar código, testes ou migrações. É necessária nova verificação do check PostgreSQL do GitHub com worker livre antes de considerar o PR verde. Não aplicar a migração no PostgreSQL operacional enquanto o gate do SHA atual estiver vermelho.
