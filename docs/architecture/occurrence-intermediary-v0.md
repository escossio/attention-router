# Intermediário de ocorrências V0 — contrato de recepção e análise colaborativa

**Estado:** DRAFT / hipótese arquitetural para revisão. Não autoriza implementação operacional, integração, deploy, consumo de IA, merge ou mudança de infraestrutura.
**Data:** 2026-10-16
**Escopo:** novo papel lógico entre detectores especializados e MAESTRO; separação do Investigador Operacional V0.
**Branch:** docs/occurrence-intermediary-v0-20261016
**Base de consulta:** main 078dd7425c839f90265428a010d30a90f66090d1 e documentos documentais existentes (referências ao final).

## 1. Decisão de direção e fronteiras

O sistema de detecção (Zabbix e demais detectores homologados) identifica anomalias. O intermediário **não monitora nem descobre falhas por conta própria**: reage a sinais autenticados, registra ocorrências e assume a **gestão da fila e da prioridade relativa de atendimento**, inclusive reavaliando casos abertos e trabalhos já iniciados à luz de novos incidentes. Usa análise colaborativa de modelos independentes para fundamentar prioridade e dependências. MAESTRO permanece responsável por orquestrar o trabalho que lhe chega **já organizado**, sem ter de recalcular prioridades nem gerir competição entre ocorrências; o Investigador V0 consulta evidências sob admissão e orçamento próprios.

A referência a uma técnica de debate entre múltiplas IAs **não implica incorporar qualquer aplicação externa, interface gráfica ou marca ao Attention Router**. Reutilizar uma ideia não significa criar dependência de runtime, biblioteca ou repositório.

Não criar um motor determinístico de árvore de decisões para deduzir causas, prioridade ou a necessidade de usar IA. O código convencional conserva responsabilidades de autenticação, integridade, normalização estrita, limites, fila, rastreabilidade, comparação estrutural de saídas e bloqueio de operações não autorizadas.

**Diretriz explicitada pelo operador em 2026-10-16:** o **intermediário é o responsável conceitual pela fila, comparação, reordenação e prioridade operacional relativa dos chamados**; essa carga **não deve recair sobre o MAESTRO**. Ele entrega trabalho já ordenado, com motivação e versões auditáveis. A prioridade de atendimento não é autorização de remediação nem de alterar um trabalho já em execução. **Conflito documental pendente:** o rascunho MAESTRO V0 anterior atribui a ele fila e prioridade; alinhar formalmente os contratos/ADR antes de codificar ou alterar a autoridade canônica no runtime. A propriedade do ticket/estado de resolução segue em aberto. Este registro não modifica os documentos MAESTRO/ILO unilateralmente.

## 2. Etapa 1 — recepção contratual, ocorrência e recibo

Entrada candidata: OccurrenceSignalV0, adaptada somente após validar contratos existentes. Campos mínimos **propostos**, não wire schema aprovado:

| Campo | Propósito | Regra provisória |
| --- | --- | --- |
| schema_version | versão reconhecida | obrigatória, exata, sem coerção |
| source_system e source_event_id | proveniência e identidade externa | obrigatórios; vinculados ao emissor autenticado |
| source_revision | distinguir atualização do mesmo alerta | obrigatório se a fonte suportar revisões; caso contrário representar ausência explicitamente |
| occurred_at | horário declarado do evento | obrigatório, com timezone |
| observed_at / received_at | observação e ingestão | recebido pela fronteira confiável, sem sobrescrever horário original |
| asset_ref ou service_ref | componente afetado | exigir pelo menos uma referência confiável, validada contra inventário quando disponível |
| signal_type e status | natureza e estado observado | enum controlado por adaptador; status não equivale a resolução |
| reason_code | diagnóstico do detector, não causa raiz | tipado, não aceitar instruções livres |
| severity_reported | severidade da fonte | opcional e distinta de prioridade |
| evidence_ref | referência rastreável de evidência | opcional, com escopo e retenção próprios |
| source-specific details | interface, porta, protocolo, container | opcionais em estrutura tipada e allowlist, não texto executável |
| correlation_id / fingerprint | vínculo e agregação | opcionais, nunca prova de causa comum |

A identidade de tenant/principal é **derivada de binding autenticado**, não do evento. O payload não seleciona escopo nem concede privilégios. O contrato real de Zabbix/webhook ainda precisa ser levantado; não declarar que a integração já está instalada.

A operação deve validar, persistir o evento original de forma restrita ou seu vínculo verificável, registrar versão e origem, produzir recibo durável e somente então confirmar aceitação. Mesma identidade + mesmos bytes/revisão: retry idempotente. Mesma identidade + bytes conflitantes: recusa/auditoria sem duplicar silenciosamente. Atualizações legítimas e eventos fora de ordem não apagam a linha do tempo.

A ocorrência deve ter identificação persistente, estado e histórico, com ligação a alertas, achados (platform/findings), eventual ticket ITSM e AFO quando aplicável. **Ainda não escolher onde reside o ticket canônico**, nem duplicar schema, histórico ou ciclo de vida que já existam.

Resultado lógico proposto da etapa 1: ACCEPTED, DUPLICATE, CONFLICT, REJECTED ou TEMPORARILY_UNAVAILABLE, com identificador/recibo quando apropriado; sem payload sensível. Uma falha no armazenamento não retorna aceite fictício.

## 3. Etapa 2 — relevância e prioridade por raciocínio colaborativo

Para uma rodada de avaliação, compor um **snapshot imutável e versionado** de ocorrências abertas/atualizadas, estados, dependências confirmadas pelo inventário, impacto/criticidade conhecidos, histórico de remediação e referências de evidências. Distinguir fato, relato de detector, hipótese, ausência e informação stale. Restringir por tenant. Não antecipar gabarito de cenários de teste.

A mesma pergunta operacional, bem especificada e sem gabarito, é enviada a múltiplos modelos de forma independente. Cada um deve classificar relevância relativa, levantar dependências prováveis, identificar prioridades equivalentes, possibilidades de aguardar outras verificações, riscos e informação faltante. Deve poder responder INCONCLUSIVE. O número de modelos, fornecedores, rounds, limites e preços não foram decididos.

**Esquema lógico candidato de resposta por modelo (não API):**

- assessment_id, snapshot_id e model_run_reference;
- ranked_occurrence_ids: lista referenciando apenas IDs válidos do snapshot, podendo conter empates;
- suspected_relations: pares de IDs, natureza da dependência, justificativa, evidências a favor e contra, marca HYPOTHESIS;
- recommended_investigation_order e suggested_defer_ids: propostas, nunca comandos;
- rationale, uncertainty, missing_evidence, contradictory_evidence_refs e outcome (PROPOSED / INCONCLUSIVE);
- provenance: modelo/versão, contrato, momento, custo/uso quando conhecido, prazo e resultado da chamada.

O comparador deve **validar esquema, IDs, versão de snapshot e correspondência de resultados**, identificar convergência e divergência, encaminhar contestação e eventual arbitragem a modelos sob orçamento. Sua função não é reimplementar em condicionais o raciocínio de prioridade. A arbitragem sintetiza evidências e discordâncias, não transforma votação em prova causal. Respostas inválidas, desatualizadas ou indisponíveis ficam registradas; nenhuma maioria concede autoridade operacional.

**Saída candidata:** PriorityAssessmentV0 com snapshot_id, prioridades relativas e empates, relações somente hipotéticas, justificativas, divergências preservadas, confiança/limitações explicadas e evidence_refs. O intermediário deve manter uma **fila de atendimento ordenada e reavaliável**, cuja titularidade conceitual é sua, e entregar demandas já organizadas ao MAESTRO. A operação precisa registrar como a avaliação por IA se tornou uma decisão de fila sob política aprovada; resultado de IA isolado não executa nenhuma mudança privilegiada. Quórum, política de aceitação, revisão humana e a representação do estado canônico ainda precisam de contrato/testes. O intermediário não executa remediações nem toma posse das tarefas executadas pelo MAESTRO.

## 4. Volumes, atualização e falhas

Os eventos são recebidos e registrados independentemente de IA disponível. A análise pode trabalhar com lotes de mudanças e snapshot de ocorrências existentes, evitando uma consulta por alerta da mesma cascata. **Tamanho, janela temporal e latência-alvo ficam sem número até benchmark.** O reagrupamento não apaga eventos.

Não assumir que todos os alertas relacionados possuem mesma causa. A interface de rede down pode explicar três falhas de contêiner, mas isso inicialmente é hipótese. O intermediário **pode reordenar o atendimento pendente** quando surgir caso novo mais relevante, inclusive enquanto outro chamado já é atendido há 10–15 minutos. Porém **não encerra chamados nem cancela/suspende uma investigação em curso** por sugestão de modelo: deve registrar a perda de prioridade do caso em andamento e, se mudança de execução for necessária, recorrer ao contrato de coordenação e à autoridade pertinente, ou solicitar intervenção humana. Diferenciar reordenação de fila de preempção de trabalho ativo.

Cada nova atualização invalida ou exige reconciliação explícita de avaliações feitas sobre snapshots anteriores; não promover resultado stale. Em indisponibilidade parcial ou total de modelos, preservar ocorrências e estado anterior auditável, sinalizar DEGRADED/INCONCLUSIVE e não inventar prioridade validada. Nenhum fallback para execução privilegiada ou despacho direto ao Investigador.

Impor controles externos de concorrência por tenant, tempo, custo, tokens, retries, circuit breaker, evidências e tamanho; consumo desconhecido não é grátis. Não copiar automaticamente política de orçamento do Investigador V0, pois pertence a outra responsabilidade.

**Exceções e intervenção humana (fase posterior):** quando houver situação atípica — exemplo ilustrativo: dez chamados disputando uma fila e prioridades/reordenações que não possam ser resolvidas com segurança — o intermediário deve poder solicitar decisão humana. Canal preferencial: notificação no aplicativo da Andy; canais complementares: Telegram e demais transportes **já homologados e disponíveis**. Notificação com resumo mínimo, IDs, urgência, alternativas e trilha de recebimento; sem expor dados sensíveis nem disparar mensagens repetidas. **Não afirmar que integração/entrega push ou Telegram já exista para esse papel.** Não implementar agora: estudar gatilhos, limiares reais, falha de entrega, deduplicação e ausência de resposta quando houver demanda comprovada. O valor 'dez' não é limite aprovado nem gatilho rígido de produção.

## 5. Ensaios sintéticos obrigatórios antes de qualquer código operacional

1. Um alerta duplicado/retry: um evento lógico preservado, sem duplicar ocorrência.
2. Mesmo event_id com conteúdo conflitante: conflito auditado, sem aceitação silenciosa.
3. Evento recebido fora de ordem e recuperação tardia: timeline íntegra; não sobrescrever snapshot novo por antigo.
4. Queda de interface seguida de três falhas de contêineres: preservar quatro fatos, recomendar testar dependência sem afirmar causa comprovada.
5. Login inválido isolado versus indisponibilidade de serviço; depois ataque privilegiado: reavaliar prioridades com justificativas, sem regra fixa por tipo.
6. Reincidência após mesma remediação: associar histórico; não reaplicar automaticamente nem afirmar causa.
7. Três respostas concordantes porém sem evidências: não promover conclusão como causa comprovada.
8. Divergência entre modelos e falha de API: registrar inconclusão, manter chamados e evitar bypass.
9. Mudança de estado durante debate: rejeitar ou reconciliar proposta baseada em snapshot obsoleto.
10. Tentativa de prompt injection em log e evento malicioso, IDs de tenant cruzados e payload exagerado: bloquear vazamento e instrução embutida.
11. Tempestade de alertas: testar backlog, idempotência, fairness e limites sem perda, antes de fixar duração de lote.
12. Chamado já em atendimento há 15 minutos versus incidente crítico recém-chegado: o intermediário reavalia e reordena o **pendente**, registra a nova comparação e sinaliza necessidade de replanejar o ativo sem interrompê-lo automaticamente; o MAESTRO não recalcula a fila.
13. Situação atípica com dez chamados concorrentes: registrar condição de possível intervenção humana, sem adotar o número como gatilho fixo ou afirmar que há transporte de notificações habilitado.

Aceite documental: separar fatos verificados, hipóteses e decisões ainda abertas; nenhuma referência falsa a API, banco ou serviço implantado. Aceite técnico futuro: testes negativos/offline por SHA, CI distribuído, revisão de autoridade e autorização separada antes de qualquer chamada real ou deploy.

## 6. Pendências a resolver com o operador

- Identidade canônica do ticket/caso permanece aberta; **prioridade relativa da fila e sua reordenação pertencem conceitualmente ao intermediário**, não ao MAESTRO. Especificar a política formal de aceitação de análises por IA e reconciliar a autoridade operacional no ADR.
- Adaptador real do Zabbix/ROC: transporte, autenticação, catálogo tipado, estado de recuperação e versão; levantamento somente-leitura.
- Como associar a estrutura platform/findings e suas ocorrências a um ticket sem criar duplicidade de autoridade.
- Escopo de contexto/dados permitido aos fornecedores de modelos, anonimização, evidência, custo e consentimento.
- Quando produzir rodada colaborativa, limites de concorrência, versões, tolerância a atraso e política de fallback.
- Como demonstrar que decisões por IA realmente superam baseline operacional simples em cenários cegos e diferentes, sem treinar prompt no gabarito.
- Reconciliação formal do documento MAESTRO V0 e do estudo ILO, retirando do MAESTRO a responsabilidade de recalcular prioridades/administrar competição de fila sem afetar sua coordenação; sem edição unilateral desses arquivos.
- Intervenção humana como evolução posterior: gatilhos atípicos, notificação por aplicativo Andy/Telegram/canais homologados, idempotência, confirmação e fallback quando o operador não responder; não implementar agora.
- Se esse papel requer entidade executável separada, reutiliza uma entidade existente ou é apenas módulo: decidir após grafo de autoridade e alcance, não por nome.

## 7. Fontes internas verificadas

- docs/architecture/maestro-operational-incident-orchestration-v0.md na branch docs/maestro-incident-control-plane-v0-20261016 (estudo, não main).
- docs/architecture/incident-lifecycle-orchestrator-v0.md na branch docs/incident-lifecycle-orchestrator-v0-20261016 (estudo, não main).
- attention_router/platform/findings.py na main, com achados, fingerprint, ocorrências e reabertura.
- docs/architecture/operational-investigator-v0-source-admission.md e contrato ADR 0021 na branch documental do Investigador: a admissão HMAC sintética não prova integração real Zabbix.
- docs/integration-transport-v0.md na main: ingresso neutro existente é para família de integrações (inbound_event), não evidência de contrato operacional Zabbix.
- PR #366 / issue #356: certificação offline do Investigador V0 não aprova alteração multi-modelo, produção ou chamada paga.

**Esta proposta preserva os contratos existentes e registra apenas o raciocínio não versionado anteriormente. Nada aqui cria serviço, chave, rota, VLAN, DB, migração, chamada paga ou autoridade nova.**
