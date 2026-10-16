# Incident Lifecycle Orchestrator (ILO) — estudo arquitetural V0

**Status:** PROPOSTA / CONCEPÇÃO — NÃO IMPLEMENTAR  
**Data:** 2026-10-16  
**Área:** Attention Router / Operações e Segurança  
**Branch de estudo:** `docs/incident-lifecycle-orchestrator-v0-20261016`  
**Nome:** *Incident Lifecycle Orchestrator (ILO)* é **nome provisório**, não decisão sobre produto, daemon ou novo motor.

## 1. Necessidade e origem

O operador identificou uma lacuna entre a **detecção de ocorrências** e a **investigação de cada ocorrência**. Uma tentativa de autenticação indevida e a queda de um serviço não podem ser simplesmente entregues em ordem de chegada ao Investigador; há concorrência, impacto, urgência, reincidências, deduplicação e capacidade finita de investigação. Também não é adequado fazer o Zabbix, a MikroTik ou cada detector conversar diretamente com o Investigador.

**Princípio da partitura:** cada componente preserva sua responsabilidade, contratos e autoridade. O objetivo é coordenar o **ciclo de vida entre componentes**, não recriar o monitoramento no Investigador.

Este documento abre **somente um estudo**. Nenhum serviço, rota, endpoint, banco, dependência, política de autorização, modelo de IA ou sistema de chamados está autorizado por ele.

## 2. O que já existe — não reconstruir

Antes de desenhar a solução, avaliar os contratos já implementados, inclusive:

- `docs/operations/afo/README.md`: **AFO** registra falhas operacionais, evidências, causa, correção, rollback, regressão e encerramento; IDs são estáveis e há estados `OBSERVED -> INVESTIGATING -> ROOT_CAUSE_PROVEN -> REMEDIATED -> VERIFIED -> CLOSED`. AFO já governa uma parte importante do **registro e fechamento**. Não criar um segundo registro canônico de falhas sem provar a necessidade, a integração e a migração.
- `attention_router/platform/governance.py`: existem `Finding`, `DiagnosisCandidate`, `RemediationProposal`, `VerificationRun`, decisões de promoção e fronteiras de autoridade. A coordenação proposta **não** adquire permissão de aplicação de mudanças.
- Zabbix/ROC/OTel/Tempo/observabilidade e inventário de serviços/hosts têm papéis próprios de captura, sinalização e evidência; verificar cobertura real antes de assumir alertas já habilitados.
- Investigador V0 e seu motor de admissão trabalham sob demanda, com consultas limitadas, evidências rastreáveis e sem autoridade automática de correção. Conferir o contrato vigente no GitHub antes de adaptar interfaces.
- Existem mecanismos de roteamento e escalonamento em outros domínios do projeto; **não presumir** que já resolvem gestão de incidentes técnicos, nem criar versões duplicadas sem inventário.

**Lacuna candidata:** não está comprovado um controlador consolidado que receba ocorrências de múltiplas fontes, governe uma fila entre incidentes, correlacione reincidências e despache trabalho aos responsáveis segundo prioridade dinâmica. Comprovar essa lacuna antes de construir.

## 3. Separação de responsabilidades

| Responsabilidade | Autoridade / componente |
| --- | --- |
| Capturar logs, métricas, traces, login negado, saúde dos serviços | Equipamento e coletores existentes |
| Identificar condições e emitir alerta técnico | Zabbix ou motor de detecção especializado |
| Normalizar/validar origem e deduplicar evento | Fronteira de ingestão/admissão governada; definição a verificar |
| Constituir caso, correlacionar, classificar, priorizar, reagendar e acompanhar SLA/idade | **Papel proposto do ILO**, ou serviço ITSM integrado |
| Solicitar investigação admitida sobre caso específico | ILO por contrato, sem transmitir segredo ou conceder autoridade ao modelo |
| Investigar e devolver hipótese/conclusão/evidências | Investigador V0 |
| Avaliar impacto, aprovar ou negar remediação/risco | Motores externos de política e aprovação existentes |
| Executar correção | Executor autorizado, distinto do Investigador e do gestor de casos |
| Verificar reparo, recorrência e fechamento | Verificador/evidências + estado governado do caso e AFO |
| Mostrar fila e trilha para operador | MGMT, com privilégios de leitura e sem controle implícito |

**Fluxo candidato:**

```text
MikroTik / serviços / CI / infraestrutura
        |
        v
Zabbix + outros detectores especializados
        |
        v
Fronteira governada de ingestão / normalização
        |
        v
ILO (PAPEL: correlação, fila, prioridade, responsáveis, histórico)
        |                 |
        |                 +--> registro/caso AFO / ITSM (reusar contratos)
        v
Admissão / orçamento / autorização da investigação
        |
        v
INVESTIGADOR (consulta evidências, NÃO monitora continuamente)
        |
        v
Hipóteses / evidências / limites / conclusão
        |
        v
Política de decisão -> executor autorizado -> verificação -> caso/AFO
        ^
        |_________ nova ocorrência ou falha recorrente __________|
```

A simples emissão de um alerta pelo dispositivo **não** cria autoridade para executar comandos, bloquear endereços IP ou mudar roteamento/firewall.

## 4. Por que não é apenas uma fila FIFO

Uma ocorrência antiga pode ser preterida por uma nova que afete a continuidade de um serviço. Prioridade **não é sinônimo de horário, volume de logs nem quantidade de tentativas**.

Proposta de critérios sob política determinística, com justificativa auditável:

- **Impacto atual** em serviço/usuários/dependências do inventário;
- **Urgência** (prazo tolerável, SLO/SLA, risco de propagação, tempo sem atendimento);
- **Segurança e risco** (ex.: login indevido isolado não é igual a ataque distribuído com indícios de êxito);
- **Confiança e integridade da fonte** (evento ausente, suspeito ou duplicado nunca vira verdade por repetição);
- **Recorrência, frequência e intervalo** entre episódios;
- **Histórico de correções e falhas de verificação**, inclusive repetição da mesma remediação ineficaz;
- **Serviços afetados e dependência topológica** (uma causa comum pode gerar alertas em muitos contêineres);
- **Custo/capacidade de investigação** e justiça de fila, com proteção contra inanição de casos antigos.

Prioridade e explicação precisam ser **recalculáveis**, versionadas e passíveis de revisão. Uma sugestão sem política aprovada não altera a prioridade canônica.

### Cenário de referência A: duas ocorrências competem

Às 11h é detectada uma autenticação inválida isolada na MikroTik. Posteriormente, uma porta crítica de serviço de contêiner deixa de responder. O ILO deve **comparar impacto, urgência e risco**, podendo alocar investigação à indisponibilidade primeiro. Se houver indícios de ataque ativo, a ocorrência de segurança pode superar a indisponibilidade: **não fixar resultado pela descrição do exemplo**.

### Cenário de referência B: mesma solução, mesma falha

1. Incidente A: serviço caiu; remediação R foi aplicada e o serviço voltou. Verificador confirmou.
2. Incidente B: mesmo sintoma e dependência, mais tarde; o histórico mostra R aplicada novamente.
3. O ILO relaciona os episódios por **evidências e fingerprints governados**, sem colapsar causas diferentes em um único registro.
4. O caso ganha sinal `RECURRENCE_AFTER_REMEDIATION`, com justificativa e histórico. O ILO **não repete automaticamente R** só porque funcionou antes.
5. A situação pode pedir **gestão de problema / causa raiz** distinta do restabelecimento do incidente, preservando AFO e a aprovação externa.
6. Corrigir um sintoma não deve encerrar uma hipótese causal nem eliminar a necessidade de verificação e de prevenção de recorrência.

### Cenário de referência C: chuva de alertas

Uma dependência de rede causa trinta alarmes em serviços distintos. A camada proposta correlaciona sem perder cada evento original; limita investigações redundantes, mantém visibilidade de impacto total e não apaga ocorrências divergentes.

## 5. Entidades candidatas (não schema)

- **Sinal/evento de origem:** ID e timestamp da fonte, escopo/autenticidade, fingerprints, classes de severidade, referências a evidências.
- **Ocorrência:** identidade imutável de uma observação classificada; pode estar associada a um caso sem ser o próprio caso.
- **Caso/incidente:** identidade governada, estado, vínculo com AFO ou ticket externo, impacto/urgência/prioridade e sua justificativa.
- **Problema:** agrupamento longitudinal de incidentes recorrentes ou causas comuns, sem substituir os incidentes.
- **Tarefa/dispatch:** responsável, orçamento, prazo, relação com investigação/execução/verificação e estado dos retries.
- **Resultado:** evidências, conclusão classificada, remediações propostas/executadas (quem aprovou), verificações, regressões e histórico versionado.

**Não** propor migrations até mapear o que AFO, PostgreSQL e plataformas ITSM existentes já persistem.

## 6. Questões arquiteturais que precisam de resposta

1. **Build × buy × integrar:** há uma plataforma consolidada que já entrega ticket, correlação, SLAs, histórico, priorização e API com isolamento suficiente? Podemos implementar apenas um adaptador de contratos.
2. Uma ocorrência de segurança pertence ao mesmo workflow de incidente geral ou exige extensão especializada (por exemplo, case management de SOC)?
3. Quem é o proprietário canônico do caso: AFO, plataforma externa ITSM, ou projeção sincronizada? Evitar duas autoridades de fechamento/priority.
4. Qual fronteira autentica detectores e transforma alertas em solicitações de investigação sem acesso direto ao LLM?
5. Quem calcula e autoriza prioridade, reclassificação, SLA e desempate? Como evitar starvation, loop de escalonamento e avalanche de incidentes?
6. Qual fingerprint permite correlação sem mascarar contextos/tenants, origens falsas, diferentes causas ou falhas novas?
7. Como representar **solução paliativa versus resolução causal**? Qual prova reabre um caso?
8. Qual relacionamento com AFO, Host Registry, fluxos CI, Zabbix, Tempo, OpenTelemetry, MGMT e Investigator? O que já é fornecido?
9. Como obter auditabilidade durável, retenção, privacidade, replay/idempotência, deduplicação e resistência a falhas do fornecedor externo?
10. Quem tem autoridade para pausar, cancelar, reordenar, aprovar execução e declarar incidentes encerrados?

## 7. Mercado a avaliar antes de construir

**Não confundir observabilidade com gestão de chamados.** São categorias parcialmente sobrepostas, mas não intercambiáveis.

| Nome levantado | Papel potencial | Parecer provisório |
| --- | --- | --- |
| **Dynatrace** | Observabilidade + correlação/AIOps e análise de problemas | Avaliar se já cobre deduplicação/impacto e como se integra ao fluxo ITSM. Não presumir que substitui gestão de casos/autoridade. |
| **Elasticsearch / Elastic Stack** | Busca, retenção/análise de logs e dados operacionais; recursos de alertas variam por produto/licença | Fonte/apoio à detecção e investigação; não adotá-lo como gestor canônico de chamados sem estudo. |
| **Prometheus** | Métricas/alertas; Alertmanager para agrupamento/roteamento de notificações | Não substitui por si só workflow completo de casos, remediações, históricos e aprovação. |
| **ServiceNow ITSM** | Incident/Problem Management, impacto × urgência, SLA, atribuição e fluxo de atendimento | Candidato de referência funcional, sujeito a custo, operação e contratos. |
| **Jira Service Management** | Incidentes, alertas, automações, integração e histórico | Candidato de referência funcional; recursos de agrupamento e preço/licenças precisam verificação. |
| **GLPI / Zammad** | Gestão de chamados e ciclo de atendimento | Avaliar API, operação própria, plugin, prioridade, recorrência, auditoria e extensibilidade. |
| **TheHive** | Case management de segurança | Candidato somente se o domínio SOC exigir casos especializados. |

**Fontes preliminares de mercado (consultadas em 2026-10-16):**
- ServiceNow: https://www.servicenow.com/docs/r/pt-BR/it-service-management/problem-management/prioritise-problems.html
- Jira Service Management, incident automation: https://support.atlassian.com/jira-service-management-cloud/docs/automate-incident-management-in-jira-service-management/
- Jira Service Management, alert grouping: https://support.atlassian.com/jira-service-management-cloud/docs/configure-alert-grouping/
- Dynatrace Problems: https://docs.dynatrace.com/docs/dynatrace-intelligence/problems-app

**Nenhum produto foi selecionado.** Essa lista não é uma comparação certificada de custos, funcionalidades ou conformidade.

## 8. Portas/contratos desejados (hipóteses, não APIs aprovadas)

A implementação futura deverá separar contratos de **entrada de sinais**, **consulta do estado da fila**, **reclassificação governada**, **despacho de investigação**, **proposta de remediação**, **feedback do verificador** e **histórico de solução**. Proibido criar neste estudo endpoints externos ou dar ao detector uma interface privilegiada diretamente no Investigador.

Políticas mínimas a especificar antes do código:

- fonte autenticada e escopo de tenant fixo;
- idempotência por mensagem e identidade canônica por caso;
- preservação do evento original, sem repassar bytes não confiáveis ao modelo;
- orçamento/limites de tarefas e proteção contra tempestade de alertas;
- razões auditáveis em toda mudança de prioridade, responsável e estado;
- nenhum bypass de aprovação por urgência/proatividade;
- confidencialidade, retenção, auditoria e provas de fechamento;
- detecção independente de falhas silenciosas de coleta como frente distinta de cobertura de observabilidade, não tarefa proativa do Investigador.

## 9. Próxima etapa *somente quando autorizada*

1. Inventariar novamente os motores/contratos existentes e identificar capacidades já satisfeitas por AFO, governança e Zabbix.
2. Escrever matriz de requisitos com 2–4 ferramentas reais, incluindo licenças, APIs, implantação, redundância e modo de falha.
3. Simular as três situações acima com regras explicáveis e sem infraestrutura nova.
4. Decidir se há necessidade de **motor próprio**, de **produto ITSM**, ou apenas de **adaptador/governança leve**.
5. Aprovar ADR, contrato de dados, fronteira de autoridade, dependências, testes e plano de implantação. Só então abrir PRs de código.

### Registro da decisão desta etapa

- Separação entre detecção, gestão de ocorrências, investigação, remediação e verificação **aceita como hipótese arquitetural de trabalho**;
- Necessidade de coordenar prioridades/recorrências **aceita como problema a estudar**, sem afirmar ausência de mecanismos equivalentes;
- Nome ILO **provisório**;
- Plataforma, schema, APIs, processos, recursos, estimativas e deployment **não decididos**;
- **Sem alteração** no trabalho em curso do Investigador, VLAN219, MGMT, Zabbix ou CI;
- Nenhuma ação automática iniciada; branch exclusivamente documental.
