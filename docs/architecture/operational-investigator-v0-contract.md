# Contrato V0 — Investigador Operacional Autônomo

**Identificador:** `AR-OPS-INV-V0`  
**Versão proposta:** `0.1.0-draft`  
**Estado:** PROPOSED — contrato para revisão, não uma declaração de funcionalidade implementada  
**Data:** 2026-10-15  
**Projeto:** Attention Router / Andy Ops  
**Autoridade:** GitHub define contrato e testes; o AGT fornece evidência operacional apenas em fase autorizada.

## 1. Resultado esperado

Investigar incidentes operacionais iniciados por um evento preservado do Zabbix ou por reprodução controlada desse evento, utilizando **um único agente investigador**, runtime de agente existente, ferramentas de consulta de menor privilégio, memória da sessão, hipóteses revisáveis e conclusão rastreável.

Não desenvolver um correlacionador central de regras, não implementar novo framework de agentes e não começar com multiagentes. As consultas e hipóteses são escolhidas dinamicamente pelo investigador; políticas de acesso, orçamento e encerramento continuam determinísticas e externas ao modelo.

**Fronteira V0:** o investigador pode escrever exclusivamente seu próprio estado/auditoria em armazenamento isolado. Toda ferramenta que toca sistemas operacionais, serviços, CI, AFO ou bancos de terceiros é **somente-leitura**. Não corrige, reinicia, publica, mescla, aprova nem configura.

## 2. Entidades de execução versus componentes internos

| Entidade de execução (container, VM ou serviço existente) | Estado V0 | Conteúdo / responsabilidade |
| --- | --- | --- |
| Investigador V0 | **Uma nova entidade prevista; não provisionada** | Runtime do agente, ciclo de ferramentas, admissões de evento, orçamento, estado de sessão, API/CLI local de replay, política e formatação da conclusão |
| Zabbix ROC | Reaproveitar, acesso a validar | Evento original, histórico, métricas e metadados observáveis |
| OpenTelemetry / Tempo | Reaproveitar | Traces, spans, vínculos e tempos entre componentes |
| PostgreSQL existente | Reaproveitamento **proposto**, condicionado a validação de isolamento | Banco/schema e principal **próprios** do investigador para sessões e auditoria; consultas operacionais por identidade distinta de somente-leitura |
| Hosts Linux / CI distribuído | Reaproveitar hosts/serviços existentes | Consultas autorizadas de processos, logs, jobs, workers e execuções, sem shell arbitrário |
| Catálogo AFO | Reaproveitar, consulta | Evidência histórica, estados e relações já documentadas; não reclassificar nem alterar AFO automaticamente |

O número de componentes internos **não** implica containers, VMs, IPs ou VLANs adicionais. Memória, conectores, auditoria e guardrails são módulos da única entidade investigadora, não novos serviços obrigatórios.

## 3. Fluxo contratual

1. Registrar origem e bytes/snapshot do evento original, quando legitimamente disponíveis, em armazenamento restrito e imutável para a sessão, com hash e proveniência. Um replay usa cópia imutável, sem modificar o Zabbix.
2. Criar sessão de investigação idempotente, atribuindo `investigation_id`, carimbo de tempo e limites explícitos.
3. Apresentar ao agente somente os dados minimizados necessários e ferramentas permitidas; entradas externas são **dados não confiáveis**, nunca instruções ou autoridade.
4. Selecionar ferramenta, executar operação permitida sob credencial de escopo próprio, registrar parâmetros sanitizados, horário, resultado/erro, fonte e identificador da evidência.
5. Atualizar memória da sessão: fatos observados, hipóteses, testes contraditórios, lacunas e próximas verificações.
6. Encerrar por evidência suficiente, impossibilidade de investigação, falha de autoridade/ferramenta ou esgotamento de orçamento.
7. Emitir relatório com fatos comprovados, hipóteses, incertezas, cronologia e referências às evidências. **Métrica normalizada não encerra incidente.**

## 4. Contratos de dados — sem fixar implementação

Os nomes abaixo são conceituais. Codex poderá definir classes e persistência compatíveis após revisar as convenções atuais; qualquer mudança semântica retorna a este contrato.

### 4.1 IncidentSeed / evento original

Campos obrigatórios:

- `source_system`: origem autenticada, como Zabbix ou replay offline.
- `source_event_id`: identificador externo preservado quando disponível.
- `observed_at` e `ingested_at`: instantes distintos.
- `source_snapshot_ref` e `source_sha256`: referência restrita ao original e digest verificável.
- `environment_scope`: escopo de hosts/ambiente derivado de autorização confiável, **não** de payload não autenticado.
- `correlation_hints`: identificadores técnicos opcionais, nunca prova causal.
- `origin_kind`: `LIVE` ou `HISTORICAL_REPLAY`; histórico não contamina incidentes ao vivo.

Se a origem não oferecer corpo original completo, registrar explicitamente essa limitação e preservar integralmente o material efetivamente recebido; não afirmar preservação dos bytes que não foram obtidos.

### 4.2 InvestigationSession

- `investigation_id` e `seed_id` (imutáveis).
- `contract_version`, `implementation_sha`, `model_id` e `agent_runtime_id/version`, quando conhecidos.
- `status`: `CREATED`, `INVESTIGATING`, `CONCLUDED`, `INCONCLUSIVE`, `BUDGET_EXHAUSTED`, `BLOCKED`, `FAILED`.
- `started_at`, `ended_at`, `budget_limit`, `budget_consumed` e referência para auditoria.
- `replay_dataset_id` opcional, com isolamento entre execução cega e gabarito.

### 4.3 ToolInvocation / Evidence

Cada chamada deve registrar `tool_name`, `tool_version`, `principal_scope`, `invoked_at`, `duration`, `query_fingerprint`, `result_status`, `source_timestamp` (se houver), `evidence_id`, `content_hash` e `redaction_status`. Parâmetros e respostas podem conter material privado: somente a representação sanitizada vai para observabilidade geral; conteúdo permitido fica em área controlada, sujeito à política de retenção.

Um erro, timeout, resposta vazia ou permissão negada é **evidência de limitação de consulta**, não prova de ausência do incidente.

### 4.4 Hypothesis / Finding

- Hipótese: `hypothesis_id`, declaração verificável, evidências a favor/contra, status `OPEN`/`SUPPORTED`/`REFUTED`/`UNRESOLVED`.
- Achado: `finding_id`, texto, classe `PROVEN`/`INDICATIVE`/`UNKNOWN`, `evidence_ids` e observação sobre causalidade.
- **Regra:** a classe `PROVEN` exige suporte identificável, verificável e suficiente; associação temporal isolada não comprova causa.
- Conclusão: síntese, limites, hipóteses descartadas, questões abertas e motivo objetivo de encerramento. O agente não declara `ROOT_CAUSE_PROVEN` em AFO nem atualiza Zabbix.

## 5. Ferramentas e permissões

| Ferramenta | Ações permitidas V0 | Proibidas V0 |
| --- | --- | --- |
| Zabbix | Listar/ler eventos, histórico e métricas em escopo autorizado | Acknowledge, fechar eventos, editar triggers, hosts ou configuração |
| Tempo/OTel | Consultar traces/spans existentes, limites de tempo e cardinalidade | Alterar collector, instrumentação, retenção ou dashboards |
| PostgreSQL operacional | Consultas parametrizadas e pré-autorizadas, preferencialmente por views de leitura | DDL, DML, superuser, acesso indiscriminado, escrita em tabelas operacionais |
| Linux | Diagnósticos por endpoints/comandos **estritamente allowlisted**, de leitura | Shell arbitrário, sudo amplo, escrita em arquivos, reinício, kill, instalação |
| CI distribuído / GitHub | Ler jobs, workflows, checks, metadados e logs sanitizados com escopo | Dispatch, rerun, cancel, publicar status, escrever código ou alterar runners |
| Catálogo AFO | Ler registros publicados/aprovados | Editar estados, criar/fechar AFO ou importar conclusões automaticamente |
| Store exclusivo do investigador | Registrar sessão, snapshot permitido, chamadas, evidências, hipóteses e conclusões | Alterar dados existentes do Attention Router, Andy ou Zabbix |

Credenciais **independentes por conector**, escopos mínimos, egress limitado, segredos fora do Git, auditoria e falha fechada. Não expor Docker socket amplo nem usar conta de administrador do host. Logs, comentários de PR, prompts em traces e respostas externas devem ser tratados como conteúdo potencialmente adversarial.

O contrato lógico de cada conector deve prever: versão, operação autorizada, escopo de acesso, filtros/limites de consulta, timeout, erros tipados, sanitização, hash/proveniência e paginação quando necessária. Sem permissões reais ou conectividade real nesta fase.

## 6. Runtime e orçamento

- Reutilizar preferencialmente uma integração de agente **já comprovada no repositório**: há adaptador opcional OpenAI Agents SDK; avaliar seu reaproveitamento para a função investigativa, isolada do fluxo de decisão da Andy.
- Codex open source, Codex SDK e OpenAI Agents SDK são candidatos/referências para comparação. Não presumir APIs, persistência ou permissões internas não documentadas.
- Controlar externamente ao modelo limites configuráveis de duração total, chamadas por ferramenta, tokens/custo, volume de resultados, profundidade de sequência e simultaneidade.
- Exigir parada segura com relatório parcial quando qualquer limite for alcançado; proibir retries infinitos.
- O resultado de cada execução referencia commit exato, versões, conjunto de ferramentas e fixture/snapshot. Replays não devem depender de horário da máquina ou estado mutável de produção.

## 7. Critérios de aceitação offline

1. Evento original/snapshot preservado e hash verificável antes de qualquer análise.
2. Sem acesso real a Zabbix, Tempo, banco operacional, Linux, CI, AGT ou roteadores em testes V0.
3. Contratos de leitura rejeitam operações desconhecidas e tentativas de escrita, com testes negativos.
4. Toda decisão substantiva referencia evidência ou declara incerteza; falta de consulta não vira ausência do evento.
5. Correlação temporal, normalização de métricas e erro de ferramenta não geram declaração automática de causa raiz.
6. Limite de tempo, chamadas, custo e volume exercitado por testes simulados; encerramento preserva o relatório parcial.
7. Fixture cega do incidente CI01, sem entregar hipótese causal, identificação do job ou resposta ao investigador. Avaliação posterior compara os achados com evidências independentes, sem promover o gabarito a prompt.
8. Repetir em outros incidentes históricos **não empregados no desenho**; registrar acertos, falsos positivos, custo e incerteza.
9. Nenhuma alteração de componentes atuais ou rede exigida para executar a suíte offline.
10. Aprovação explícita dos contratos, controles e checks **antes** de considerar implantação AGT.

## 8. Versionamento e evolução

- `0.1.0-draft`: baseline proposto, sujeito a revisão.
- Após aprovação: fixar baseline V0 e commit/PR correspondente.
- Alterações incompatíveis no significado de campos, autoridade ou fluxo exigem revisão explícita e versão incompatível; extensões opcionais são versionadas separadamente; correções editoriais não alteram semântica.
- Módulos futuros são aditivos, desde que respeitem escopos de leitura, proveniência e causalidade.
- AFO permanece registro governado independente, sem substituição de seu protocolo de remediação e fechamento.

## 9. Referências locais e conflitos evitados

- `AGENTS.md`
- `docs/operations/github-first-development-control-loop.md`
- `docs/architecture/container-communication-network-placement-v1.md`
- `docs/architecture/channel-context-dual-track-v1.md`
- `docs/ci-agent-v0.md` (CI Agent existente é **outra responsabilidade**)
- `docs/operations/afo/README.md`
- `docs/observability/live-flow.md`
- `docs/architecture/operational-investigator-v0-communication-graph.md`
- `docs/operations/operational-investigator-v0-rollout.md`

**Não objetivos da fase documental:** criação de containers/VMs, rede/VLAN/IP, migração de banco, provisionamento de credenciais, deploy, remediação, roteamento, modificação da MikroTik ou alteração da `main`.
