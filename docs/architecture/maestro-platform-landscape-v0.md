# MAESTRO — Estudo inicial de mercado para gestão de incidentes

**Status:** comparação preliminar, não é seleção de produto e não autoriza instalação.  
**Data de consulta:** 2026-10-16. **Referência:** [Contrato MAESTRO V0](../architecture/maestro-operational-incident-orchestration-v0.md).

## 1. Qual problema a ferramenta precisa resolver?

Uma ferramenta ou composição de ferramentas existente no mercado deve assumir ou apoiar o **estado persistente do caso, priorização, deduplicação, agrupamento, reatribuição, escalonamento, SLA, vínculo de recorrências, histórico de correções e encerramento verificável**. O Investigador recebe demandas já admitidas e devolve análise; não vira um helpdesk.

É indispensável separar:

- **Event Management / AIOps:** consolida alertas e indica quais incidentes importam.
- **Incident Management / ITSM:** garante proprietário, prioridade relativa, fila, prazos e restauração do serviço.
- **Problem Management:** coordena investigação da causa de incidentes recorrentes, erros conhecidos e correção definitiva.
- **Change / Execution Management:** organiza e governa as mudanças autorizadas. Um chamado não dá automaticamente privilégios de executar comandos.

## 2. Produtos mencionados na discussão

| Tecnologia | Vocação comprovada na documentação | Aderência à lacuna | Risco de escolher agora |
| --- | --- | --- | --- |
| **Dynatrace** | Observabilidade, correlação Davis de eventos, análise de causa e impacto, problemas e workflows | **Alta em Event/AIOps**, com elementos do ciclo de incidentes; candidato a integração, não sinônimo de ITSM completo | Redundância de telemetria/custo/overlap com Zabbix/ROC; funcionalidades/licenciamento não verificados no AGT |
| **Elasticsearch / Elastic Observability + Cases** | Pesquisa de logs/métricas/traces; alertas, Cases e conectores com Jira, ServiceNow, PagerDuty | **Boa para enriquecer casos e investigar**, especialmente se Elastic já for a base de dados; também suporta caso próprio | Operação de stack nova apenas para chamados pode ser excessiva; verificar módulos e versões |
| **Prometheus + Alertmanager** | Métricas; deduplicação, agrupamento, supressão e roteamento de alertas | **Alta em roteamento de alertas**, mas **insuficiente como gestor integral de chamados e problemas** | Montar Prometheus não cria Problem Management nem gestão do ciclo de vida |

**Conclusão provisória:** entre os três nomes lembrados, Dynatrace e Elastic oferecem funções de correlação/gestão relacionadas; Prometheus sozinho não resolve a lacuna de chamados. Não instalar nenhum dos três antes de avaliar o que já opera no projeto.

## 3. Plataformas do segmento específico que devem entrar na avaliação

| Categoria/produto | Função de interesse | Ponto a investigar |
| --- | --- | --- |
| **ServiceNow ITSM + ITOM Event Management** | Incidente, Problem Management, associação e correlação de alertas, modelos ITSM | Custo, API, SaaS, isolamento/autorizações e pertinência ao laboratório |
| **GLPI** | Chamados conforme ITIL, impacto, urgência, matriz de prioridade e ciclo do incidente | API disponível, modelo de Problems, licenças/plugins e operacionalização sem serviço duplicado |
| **PagerDuty** | Event Orchestration, `dedup_key`, priorização, escalonamento e Incident Workflows | Foco em incident response/on-call; quando precisa combinar com ITSM/AFO |
| **Elastic Cases**, caso o stack Elastic já esteja presente | Casos e integração com sistemas externos | Viabilidade de integração sem implantação de um stack paralelo |

Não avaliar produtos apenas por número de recursos: foco em estabilidade, integração a Zabbix, auditabilidade de decisão e custo operacional.

## 4. Critérios de escolha — antes de instalar

1. **Reuso:** identifica solução já implantada, incluindo Zabbix/ROC/AFO/CI Agent/registries, sem reimplementar seu contrato.
2. **Integração:** recebe alertas via interface confiável e emite eventos de caso auditáveis; não fornece acesso de escrita indiscriminado aos detectores.
3. **Prioridade dinâmica:** separa `severity`, `impact`, `urgency`, `priority` e `recurrence`, com histórico e política explicável.
4. **Correlação com contenção:** idempotência, agrupamento e links sem supor causa raiz comum sem evidência.
5. **Ciclo de vida:** owner, acknowledge, escalonamento, pausa justificada, resolução, verificação e reabertura.
6. **Problem Management:** recorrência após mesma remediação → problema/AFO associado, com reanálise da causa.
7. **Governança:** integra motor de admissão e aprovação já existente, segregação tenant, RBAC, no bypass.
8. **Custo/operabilidade:** licença, consumo, hardware do AGT, infraestrutura adicional e modo de deploy; evitar stacks pesadas se adaptador leve atende.
9. **Observabilidade:** cada decisão visível no MGMT sem vazar payload/segredo; perda da camada de chamados cria alerta de controle.

## 5. Decisões ainda abertas

- MAESTRO é **nome de trabalho** do papel arquitetural, não produto selecionado.
- Uma ferramenta externa poderá ser **sistema de registro e workflow**; o adaptador do Attention Router deverá ser mínimo e governado.
- AFO permanece acervo de evidência e regras de fechamento enquanto sua relação com o registro ITSM é definida.
- Não decidir contêiner, rede, credenciais, banco, fornecedor nem scheduler nesta branch.
- Próximo passo permitido após priorização pelo operador: PoC documental de encaixe com **Zabbix -> plataforma ITSM/Event Management -> admission -> Investigator**, sem tráfego real.

## 6. Referências primárias (consultadas em 2026-10-16)

- Dynatrace — https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts
- Elastic Cases — https://www.elastic.co/docs/solutions/observability/incident-management/observability-cases
- Elastic Alerting/Cases Connectors — https://www.elastic.co/docs/reference/kibana/connectors-kibana/alerting-cases-connectors
- Prometheus Alertmanager — https://prometheus.io/docs/alerting/latest/alertmanager/
- PagerDuty Event Management — https://docs.pagerduty.com/incident-management/incidents/event-management
- ServiceNow Problem Management — https://www.servicenow.com/docs/r/it-service-management/problem-management/create-a-problem-v2.html
- GLPI Tickets — https://help.glpi-project.org/documentation/modules/assistance/tickets/ticketmanagement
