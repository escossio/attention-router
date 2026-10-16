# MAESTRO V0 — Orquestração de ocorrências, chamados e problemas operacionais

**Estado:** PROPOSTA / estudo arquitetural, **não implementado, não autorizado para deploy**  
**Data:** 2026-10-16  
**Frente:** isolada do Investigador V0 e de sua cadeia de PRs  
**Nome de trabalho:** **MAESTRO** — camada de comando de casos operacionais. Nome sujeito a revisão.

## 1. Problema que motivou a proposta

Não é responsabilidade do Investigador Operacional V0 monitorar continuamente MikroTik, Linux, Zabbix, containers, traces ou logs para produzir a própria demanda. Detectores especializados já têm esse papel. Tampouco cada detector deve acionar diretamente um Investigador sem controle intermediário.

Exemplo: às 11h uma tentativa de autenticação inválida na MikroTik gera um evento; depois, uma porta de serviço de container falha. **Qual ocorrência deve ser atendida primeiro?** A resposta depende de impacto, criticidade do serviço, risco de segurança, alcance, urgência, dependências, recorrência e evidência disponível — não da ordem de chegada, do nome da fonte ou de um score isolado. Uma tentativa isolada de login pode ser rotineira; uma campanha de acesso privilegiado contra o plano de controle pode ser prioridade máxima. Não congelar o exemplo como regra geral.

A lacuna é a **titularidade do ciclo de vida da ocorrência**: admissão, fila, classificação, correlação, priorização relativa, despacho, acompanhamento, escalonamento, verificação e fechamento. Isso é disciplina de **ITSM / Incident Management e Problem Management**, não mais uma camada de observabilidade duplicada.

### Fluxo conceitual (não implantado)

```text
MikroTik / Docker / Aplicações / CI
       ↓ telemetria coletada e detecção
Zabbix / ROC / OTel / outros detectores homologados
       ↓ evento tipado, com identidade e proveniência
[ ADMISSÃO AUTENTICADA DE OCORRÊNCIAS ]
       ↓
[ MAESTRO — autoridade sobre o CASO operacional ]
       ├─ deduplicação, correlação e impacto
       ├─ fila dinâmica + prioridade + prazo
       ├─ histórico e recorrência (incidente ↔ problema/AFO)
       ├─ despacho idempotente e acompanhamento
       └─ trilha explicável de cada mudança
       ↓ trabalho especializado e limitado
INVESTIGADOR V0 / outros responsáveis
       ↓ hipóteses, evidências e propostas, sem autoridade de executar
MOTOR DE AUTORIZAÇÃO + EXECUTOR responsável (se aplicável)
       ↓ prova de efeito, recuperação, regressão e observabilidade
MAESTRO ↔ AFO / registro de problema ↔ verificação / encerramento
```

**Invariantes:** o detector não faz bypass direto para o Investigador; o Investigador não se autoautoriza, não se autodespacha e não executa remediação; o MAESTRO não concede autoridade operacional e não determina causa raiz sem evidência; retorno ao normal ≠ causa raiz comprovada; alertas duplicados não viram dezenas de trabalhos de investigação.

## 2. O que já existe — não reconstruir

1. **AFO** em `docs/operations/afo/README.md`: registro permanente de falhas com estado `OBSERVED -> INVESTIGATING -> ROOT_CAUSE_PROVEN -> REMEDIATED -> VERIFIED -> CLOSED`, evidência, correção, rollback, regressão e medição. Identificadores AFO são estáveis; sintomas ligados à mesma causa não exigem outra AFO. O novo motor deve **integrar-se a esse acervo**, não duplicá-lo como se ele não existisse.
2. **Investigador V0** nas frentes #344 e #362–#366: capacidade especializada de investigar caso admitido sob grants, consultas auditadas, limite de custos, evidências e conclusão; em laboratório e ainda NO-GO para produção.
3. **CI Agent V0** em `docs/ci-agent-v0.md`: exemplo existente de ingestão de eventos de workflow, correlação exata de SHA e triagem com trilha restrita; **não** é o gerente universal de ocorrências operacionais.
4. **Cognitive Attention and Salience V2H** em `docs/architecture/personal-context-v2h-attention-salience-v0.md`: já formula score determinístico, distinção score/effective class, dedup e autoridade externa para **atenção de contexto pessoal**. Serve como referência de princípios, não como licença para misturar a semântica ou reutilizar sua autoridade nos incidentes de infraestrutura.
5. **Zabbix/ROC/MGMT/OTel** são produtores/consumidores de observabilidade existentes; sua cobertura e as integrações concretas precisam ser verificadas no estado real antes de desenhar um adapter.

**Esta documentação não é prova de que MAESTRO já existe**, nem autoriza inventar tabelas, endereços de rede, secrets, APIs ou serviços.

## 3. Três identidades diferentes

- **Evento/alerta:** observação de detector, com semântica temporal e proveniência. Pode ser benigno, falso positivo ou parte de um incidente.
- **Incidente / caso / chamado:** unidade operacional rastreável, com proprietário, prioridade, SLA/prazo e estado; pode agregar múltiplos alertas e despachos. Restaurar serviço reduz impacto, mas não comprova causa raiz.
- **Problema:** investigação longitudinal da causa subjacente a um ou vários incidentes, sobretudo reincidentes. Pode apontar para AFO existente, criação autorizada de nova AFO ou registro externo de Problem Management. Exige validação causal e solução definitiva; não é sinônimo de chamado.

Não confundir **deduplicação de evento**, **correlação de incidentes** e **prova de causa comum**. Proximidade temporal ou fingerprint parecido não provam identidade causal.

## 4. Contrato candidato de entrada (somente estudo)

`OccurrenceSignalV0`, vindo de adaptador aprovado, inclui campos tipados:

- `source_system`, `source_event_id`, `source_revision`, `observed_at`, `occurred_at`;
- identidade autenticada do emissor e escopo confiável do ativo/serviço (não aceitos de texto arbitrário de logs);
- `signal_type`, `severity_reported`, `asset_ref`, `service_ref`, `status`, `evidence_ref`, `fingerprint`;
- versão do esquema e vínculo auditável com origem, registro AFO/Problem/Incident quando existirem.

Autenticação do emissor, autorização por escopo, limites de payload, validação de replay/nonce, expiração, dedup determinística e fila durável são **gates prévios**. O MAESTRO não receberá logs brutos como instruções. Cada fonte se integra por adapter governado; não criar acesso direto indiscriminado de rede.

## 5. Responsabilidades candidatas

**Admissão e classificação:** aceitar/rejeitar evento autenticado, ligar a um caso existente ou abrir novo caso. Preservar evento mesmo quando a notificação for suprimida. Sem fallback automático para o Investigador caso o MAESTRO esteja indisponível; mensagem pendente em fila durável, alarmar degradação.

**Fila e prioridade dinâmica:** separar severidade do detector, prioridade operacional do caso e risco de segurança. Proposta explicável por componentes (impacto/serviço crítico, urgência/tempo, alcance, recorrência, risco, criticidade dependente, confiança dos dados, idade da fila e política de serviço). Recalcular quando surgem evidências, sem apagar valor anterior ou justificativa. Desempate determinístico, aging e guardas contra starvation e oscilação de prioridade. Nenhuma fórmula ou peso aprovado nesta versão.

**Agrupamento causal conservador:** eventos iguais (mesma chave e versão) são idempotentes; eventos relacionados entram no mesmo caso com grau de correlação, sem alegar causa comum quando não houver prova. A queda da porta pode ter precedência sobre a autenticação inválida, mas uma sequência de tentativas privilegiadas pode ultrapassá-la conforme impacto e política explícitos.

**Despacho:** emitir demanda limitada e rastreada ao Investigador V0 somente após admissão/autorização externa, com lease e idempotência; não enviar alertas diretamente ao endpoint do modelo. Prioridade pode preemptar a fila pendente; interrupção de investigação já iniciada requer contrato de checkpoint/cancelamento, não matar arbitrariamente uma execução.

**Resposta e recorrência:** associar tentativa de remediação ao caso, evidência produzida, executor, autorização, versão do runbook, efeito medido e janela de verificação. Se o mesmo sintoma reaparece após a mesma remediação, elevar suspeita de solução paliativa e abrir/agravar um **Problem** vinculado à AFO — sem inferir automaticamente a causa. Guardar frequência e histórico temporal, incluindo mudanças do ambiente desde a correção anterior.

**Conclusão:** encerramento de incidente depende da recuperação sustentada e prova operacional; fechamento de problema/AFO exige causa, correção, regressão e demais critérios da AFO. "Alerta normalizou" não fecha automaticamente o problema.

## 6. Exemplo de sequência para estudo e teste

1. 11:00: detector registra tentativa de login inválida na MikroTik; MAESTRO recebe sinal autorizado e forma caso A em fila.
2. 11:03: detector registra falha de porta em container de serviço crítico; caso B ganha prioridade porque seu impacto comprovado é maior, dentro da política vigente.
3. 11:05: várias tentativas privilegiadas associadas ao caso A elevam o risco; MAESTRO recalcula prioridades. Pode ultrapassar B se evidência e contexto justificarem.
4. 11:08: Investigador retorna hipóteses e observações do caso B; aprovação e execução pertencem a mecanismos próprios.
5. Dia seguinte: caso B recorre após aplicação da mesma correção. Abrir/vincular Problem/AFO, registrar `RECURRENCE_AFTER_SAME_REMEDIATION`, revisar eficácia, não replicar a mesma ação automaticamente.

**Testes exigidos:** replay idempotente, evento fora de ordem, concorrência, queda de fila, mudança de prioridade e injustiça, incidentes relacionados mas causalmente distintos, recorrência após suposta correção, false positive, mesma falha com outra correção, perda de observabilidade, retomada de execução e escalonamento. Somente fixtures sintéticas V0.

## 7. Interfaces e autoridade

| Fronteira | Envia / recebe | Pode executar ou autorizar? |
| --- | --- | --- |
| Detector Zabbix/ROC/OTel | Emite evento e evidência referenciada | Não |
| Adapter / admissão | Autentica, normaliza e entrega ocorrência | Não concede remediação |
| MAESTRO | Decide fila/caso/prioridade/despacho sob políticas externas | Não remedia nem eleva privilégio |
| Investigador | Consulta fontes read-only e devolve hipóteses/evidências | Não |
| Motor de autorização | Valida ator/escopo/política por operação | Somente dentro de seu contrato |
| Executor homologado | Executa ação permitida e devolve prova | Somente ação autorizada |
| AFO / Problem record | Retém aprendizado, solução, regressão e causalidade | Não |

Sem conexão direta da MikroTik com o Investigador. A MikroTik deve fornecer telemetria aos detectores homologados; a admissão unifica o encaminhamento tipado.

## 8. Questões a decidir antes de qualquer implementação

1. Adotar **plataforma ITSM/Incident Management já madura** como autoridade de casos, com adaptadores limitados, ou manter autoridade de estado mínima interna conectando-se a plataforma externa? Não criar ticket engine de raiz sem estudo de mercado/custo.
2. AFO é projeção auditável do registro de Problem, ou o Problem externo referencia AFO? Resolver identidade, ownership e conflitos sem duplicar fonte de verdade.
3. Qual é o esquema de identidade de serviço e dependências já suportado pelo registry existente? Não duplicar CMDB sem análise.
4. Como um alerta Zabbix efetivamente chega à admissão e qual é seu limite de autoridade? Checar integração e contratos atuais.
5. SLA, impacto versus urgência, score, preempção, quotas e estado do caso requerem ADR e cenários de teste.
6. Estratégia de persistência, isolamento entre tenants, retenção e auditoria precisam ser decididas antes de operação real.
7. Define-se o grafo de comunicação antes de contêiner/IP; **nenhuma VLAN, IP dedicado ou rota são necessários por esta proposta documental**.
8. Fluxo **live** e importação **histórica** devem ser analisados separadamente, com cursor, janela temporal, dedup e autorização próprios. A ingestão histórica AFO não pode corromper o estado live do caso.

## 9. Escopo e não objetivos da branch

- Esta branch inclui **somente documentação** de um estudo, sem implementação, instalação, migração, API, service unit, contêiner, credencial ou troca do roteador.
- Não alterar o Investigador em desenvolvimento nem seu ciclo de ensaios. Manter as frentes isoladas.
- Não assumir que Dynatrace, Elastic, Prometheus, ServiceNow, GLPI ou PagerDuty esteja instalado. Seleção de produto é uma decisão posterior, com PoC isolada e critérios.
- Constituição de trabalho: o debate de **hipóteses divergentes independentes** abriu esta lacuna e merece futura proposta isolada de governança do raciocínio; **não implementar aqui**, para não misturar frentes.

## 10. Referências existentes e externas

Internas: `docs/operations/afo/README.md`, `docs/ci-agent-v0.md`, `docs/architecture/personal-context-v2h-attention-salience-v0.md`, `AGENTS.md`, ADR Investigador #344 (proposed).

Externas (fontes oficiais, verificadas em 2026-10-16):

- Dynatrace: https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts
- Elastic Cases: https://www.elastic.co/docs/solutions/observability/incident-management/observability-cases
- Prometheus Alertmanager: https://prometheus.io/docs/alerting/latest/alertmanager/
- PagerDuty Event Management: https://docs.pagerduty.com/incident-management/incidents/event-management
- GLPI ticket management: https://help.glpi-project.org/documentation/modules/assistance/tickets/ticketmanagement
- ServiceNow Problem Management: https://www.servicenow.com/docs/r/it-service-management/problem-management/create-a-problem-v2.html
