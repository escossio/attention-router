# Plano de implantação por etapas — Investigador Operacional V0

**Contrato:** [AR-OPS-INV-V0@0.1.0-draft](../architecture/operational-investigator-v0-contract.md)  
**Grafo:** [entidades e fluxos](../architecture/operational-investigator-v0-communication-graph.md)  
**Data:** 2026-10-15  
**Estado:** PLAN ONLY — nenhuma implantação autorizada

## Objetivo

Formalizar a estrada (módulo, interfaces, limites, CI e pontos de extensão) antes de completar bifurcações (endpoints, redes, políticas de coleta, retenção e detalhes do runtime). Dois trabalhos independentes em paralelo: documentação/contratos via GitHub e estrutura-base/testes simulados via Codex, cada qual em sua branch. O PR de implementação **não** pode pressupor que um contrato em rascunho já foi aceito.

## Ordem e portas de controle

| Etapa | Onde ocorre | Entrega verificável | Porta para avançar | Mudança operacional |
| --- | --- | --- | --- | --- |
| E0 — levantamento | GitHub, leitura | Inventário de reuso: SDK existente, Tempo/OTel, Zabbix, AFO, Postgres, CI | Verificar código e regras existentes; mapear lacunas | Não |
| E1 — contrato e grafo | Branch documental + PR draft | Contrato V0, grafo, ADR e plano versionados | Revisão de segurança, autoridade, modelo de dados, origem/evento e rede | Não |
| E2 — esqueleto paralelo | Branch própria Codex | Módulo isolado, interfaces, stores simulados, adapters mock, testes mínimos; relatório de compatibilidade do runtime | Compilar, lint, testes offline; interfaces aderentes ao contrato aprovado ou diferenças explicitadas | Não |
| E3 — certificação GitHub | Actions + workers de CI existentes | Checks públicos, CodeQL, testes negativos, isolamento entre componentes, evidence bundles sintéticos, relatório de SHA | Checks aplicáveis verdes, gate pesado via CI distribuído, nenhuma regressão e PR aprovado | Não |
| E4 — laboratório histórico cego | CI offline com fixtures | Replay CI01 sem causa antecipada + outros incidentes holdout; medições de evidência/erro/orçamento | Evidência rastreável, sem escrita operacional ou vazamento do gabarito; avaliação humana | Não |
| E5 — preparação operacional | GitHub + inspeção AGT **somente após autorização** | Matriz real de alcance, papéis, plano de migração separado, defaults disabled, rollback | Aprovação de credenciais/rede, isolamento, observabilidade, retenção e recuperação | Inspeção somente-leitura com autorização |
| E6 — piloto controlado | AGT **somente após autorização separada** | Uma entidade investigadora isolada, inicialmente desligada; teste shadow de leitura; registro do antes/depois | Aceite operacional explícito e rollback demonstrado | Sim, etapa futura |

**Proibição de antecipação:** E0–E4 não criam Docker/VM, não alocam IP/VLAN, não alteram MikroTik, não migram Postgres operacional e não acessam AGT para implantação.

## Trilha A — contratos e governança (GitHub)

1. Tornar o significado de **entidade** explícito: limite executável (container/VM/host/serviço); módulos não viram entidades automaticamente.
2. Versionar os campos conceituais do evento, sessão, evidência, hipótese e conclusão.
3. Definir esquema de autorização de cada ferramenta: operação permitida, principal, escopo, saída sanitizada, falha/timeout e logs.
4. Definir orçamento configurável com limites objetivos e encerramento parcial reprodutível.
5. Exigir grafo por iniciador, destino, zona de confiança, protocolo, porta, autoridade, L2/L3 e rollback.
6. Registrar decisões imutáveis em ADR; detalhes ainda não verificados ficam **abertos**, não são preenchidos por suposição.
7. Garantir que a distinção live/histórico não seja apagada: o replay histórico é segregado do caminho de evento vivo.

## Trilha B — Codex / preparação da estrada (branch separada)

1. Inspecionar o adaptador OpenAI Agents SDK já existente e comparar com candidatos públicos do ecossistema Codex. Entregar matriz **confirmado/pendente/não disponível**, com versão/API citadas.
2. Criar limite claro de módulo: `EventAdmission`, `InvestigationRunner`, `ToolPolicy`, `EvidenceRecorder`, `SessionMemory` e `Conclusion` são **responsabilidades lógicas**, não exigem 6 processos/containers.
3. Criar interfaces de seis conectores sem serviços reais: Zabbix, Tempo, PostgreSQL operacional, Linux, CI distribuído e AFO.
4. Testar payload adversarial (prompt injection em log), timeout, falha de consulta, orçamento esgotado, retry controlado e tentativas de operação proibida.
5. Implementar persistência in-memory/fake; migrações reais aguardam aprovação da decisão database versus schema.
6. Produzir PR em rascunho **sem merges automáticos**, referenciando versão de contrato consumida e divergências.

**Integração entre trilhas:** Codex não deve editar os mesmos arquivos normativos em paralelo. Alteração semântica proposta pelo Codex vira comentário/issue/PR de contrato, não mudança unilateral. Revisar e fixar o SHA do contrato antes de promover a implementação.

## Gates concretos

### Gate G0 — contrato congelável

- [ ] ADR e contrato revisados, sem novos privilégios ocultos
- [ ] Matriz de fluxos assinada/revisada, inclusive potencial saída ao provedor do modelo
- [ ] Causalidade separada de correlação, severidade e normalização de métricas
- [ ] Critérios de aceitação negativos e versão de contrato documentados
- [ ] Nenhum inventário de rede particular, segredo ou payload privado versionado

### Gate G1 — código offline

- [ ] Módulo isolado sem side effects na importação
- [ ] Operações mutantes externas ausentes e rejeitadas por teste
- [ ] Eventos de replay preservados sem edição
- [ ] Auditabilidade de **todas** as consultas, inclusive recusas, falhas e timeout
- [ ] Orçamento efetivamente imposto fora do raciocínio do modelo
- [ ] Proposta de runtime respaldada por interfaces públicas verificadas
- [ ] Sem shell arbitrário, acesso amplo a Docker socket ou credenciais reais

### Gate G2 — CI e validação cega

- [ ] Lint/testes offline e checagens arquiteturais verdes
- [ ] CI distribuído executa carga pesada no SHA exato, sem fallback pesado no control plane
- [ ] Fixture CI01 sem pistas de causa entregues ao agente
- [ ] Outros incidentes holdout não usados para ajustar o mecanismo
- [ ] Relatório separa prova, indício, erro e desconhecido
- [ ] Sem vazamentos de logs sensíveis em artefatos de CI

### Gate G3 — avaliação para AGT

- [ ] Autorizações operacionais explícitas para fase somente-leitura
- [ ] Matriz real de acessos/portas aprovada
- [ ] Mesmo cluster PostgreSQL aprovado por capacidade, identidade e isolamento; namespace lógico dedicado
- [ ] Defaults disabled e rollback exercitável
- [ ] Nenhuma expectativa implícita de configuração da MikroTik
- [ ] Métrica de sucesso e parada do piloto acordadas

## Regra de permanência do PostgreSQL

Reaproveitar o cluster existente é a proposta inicial. A escolha **database próprio vs schema próprio** é uma decisão posterior baseada em isolamento, backup/restauração, migrações, papéis e capacidade. O investigador precisa de duas autoridades diferentes: leitura estrita para fontes operacionais e escrita exclusivamente no seu próprio armazenamento. Nunca promover a conta que escreve auditoria a escritora nos dados que ela investiga.

## Laboratório de replays

- Construir conjunto de entrada a partir de evento/snapshot legítimo, normalizado apenas para privacidade, com hash e proveniência; armazenar fixtures publicáveis **sintéticas** no repositório.
- Separar **dados disponíveis ao agente** de **gabarito mantido pelo avaliador** em diretórios/autorização distintos. Teste cego não apresenta hipótese de carga de CI ao investigador.
- Avaliar no CI01 se o investigador identifica a execução de CI **somente quando evidências permitirem**.
- Executar incidentes holdout sem redesenhar prompts/conectores após visualizar a resposta; registrar taxa de casos `PROVEN` sustentados, falsos positivos, evidências recuperadas, chamadas, latência, custo, falhas e `INCONCLUSIVE`.
- Não classificar ausência de dados como recuperação, inexistência ou causa comprovada.

## Política de promoção

**Contrato em revisão ≠ implementação validada ≠ runtime implantado ≠ incidente resolvido.** Cada avanço exige prova própria, PR dedicado e autorização. A `main` protegida permanece intacta até gates concluídos e revisão. GitHub mantém contratos, testes, resultados e decisão; AGT será usado apenas como evidência de runtime nas etapas E5/E6.

## Fora do escopo

- Alterações na MikroTik (responsabilidade exclusiva do operador)
- Reservas IP, alterações em VLAN, firewall ou bridges
- Deploy/provisionamento no AGT durante E0–E4
- Ações de correção automática
- Novo framework geral de agentes ou múltiplos investigadores V0
- Reescrita do atual CI Agent, SDK da Andy ou registros AFO
