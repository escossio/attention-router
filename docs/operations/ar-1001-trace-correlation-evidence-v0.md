# AR-1001 — níveis de correlação entre Zabbix, Andy e OpenTelemetry

**Checkpoint:** 18/10/2026 UTC. **Estado:** auditoria somente leitura + contrato puro offline. Não é detector novo, nem instalação MGMT, nem diagnóstico causal. Fonte viva continua íntegra; esta PR não altera o tráfego da Andy, seus bancos, Relay, MAESTRO, Zabbix, Tempo ou a sonda.

## Problema que o contrato resolve

O Zabbix possui uma ocorrência PROBLEM persistente para `ANDY_WA_AUTO_REPLY_STALLED` e quinze contadores de janela (6h) que estão operacionais. **O código AR-1001 é uma classificação de sintoma, não o identificador de cada mensagem ou da operação que falhou.** O mesmo `interaction_id` pode ser referenciado por várias entradas/auditorias, com `correlation_id` próprios distintos. Uma busca por horário ou pertencimento à mesma interação **não** equivale a um trace da operação causadora.

O panorama geral NETWORK/OSI (domínios/vizinhanças) também não define o percurso de rede da interação. Tempo disponível e traces genéricos não bastam.

## Achados observados em consulta privada somente-leitura

- A consulta de seis horas do PostgreSQL encontrou **48 eventos diretos de entrada**, **23 interações distintas** e **48 valores distintos de correlação na entrada** no instante observado. Não houve novas mensagens de saída criadas na janela consultada. Contadores variam com o tempo.
- Nos últimos 45 minutos de uma consulta anterior não havia eventos diretos novos; isso não contradiz a janela de seis horas. Não extrapolar para ausência de tráfego de todo o sistema.
- O Tempo `/ready` e sua busca retornaram HTTP 200, com traces reais de Transport/Worker. Nas primeiras amostras genéricas não existia atributo de correlação.
- Uma busca delimitada em **oito correlações de entrada pertencentes a interações com auditoria de falha** encontrou **uma correspondência exata** com `roc.correlation_id` em spans obtidos do Tempo, incluindo `attention.message` e etapas `roc.*`. Isso prova vínculo com **a entrada daquela interação**, não com a operação que registrou a auditoria nem com causa raiz.
- Uma verificação independente comparou IDs de correlação de entrada versus de auditoria para a mesma interação. Na amostra de seis horas, **nenhuma correspondência direta** foi observada entre os respectivos `correlation_id`; a relação disponível entre os dois conjuntos é `interaction_id`. Assim não se pode promover um vínculo de entrada para vínculo com a operação da falha.
- A tentativa posterior de consultar diretamente os IDs de correlação das auditorias no Tempo foi bloqueada pela ferramenta remota. **Não foi contornada**. Não afirmar que esses traces existem ou não existem.
- A referência da ocorrência original no Zabbix pertence a um *trigger* persistente, não a uma tabela que mapeie um evento específico a cada entrada. O vínculo ao próprio `Zabbix eventid` não foi comprovado. Consultas READ ONLY executadas usaram IDs apenas em memória e **não emitiram** PII, valores de correlação ou identificadores privados na saída.

## Escalonamento de evidência (V0)

| Nível | Prova exigida | O que **não** prova |
| --- | --- | --- |
| `REJECTED_SCOPE` | Fonte, tenant, interação ou evento fora do escopo | Nada sobre o trace |
| `UNKNOWN` | Dado ausente, inválido, stale ou consulta inacessível | Não significa zero traces |
| `NOT_PROVEN` | Não há atributo verificado ou alinhamento temporal válido no trace consultado | Não prova inexistência de tracing |
| `SAME_INTERACTION_INPUT` | Mesmo tenant e interação na base, `roc.correlation_id` idêntico ao da entrada em span efetivamente recuperado, dentro da janela | **Não** liga o span ao registro de auditoria que detectou falha |
| `SAME_AUDIT_CORRELATION` | Mesmo tenant/interação + atributo `roc.correlation_id` do span igual ao evento da auditoria de falha e tempo compatível | Ainda **não** prova causa raiz, Zabbix event ID ou autorização para resolver |

O classificador puro está em `ops/observability/failure_catalog/ar1001_correlation_evidence.py` e tem testes sintéticos em `tests/test_ar1001_correlation_evidence.py`. A saída **nunca** contém UUID, ID de interação/tenant, `trace_id`, IP privado, payload, corpo de mensagem ou URL interna. `causal_origin_proven=false` e `zabbix_event_member_proven=false` são invariantes V0. O campo audit_correlation_matched=true certifica igualdade do UUID de auditoria no span, mas failure_stage_proven=false ainda reconhece que a etapa causadora não foi identificada.

Os nomes de span são de uma allowlist curta. Um hit da busca `/api/search` não serve sozinho: a regra exige buscar o trace por ID e verificar no conteúdo do span o atributo idêntico ao recebido da fonte autorizada. Não aceitar um trace aleatório cujo timestamp apenas coincida.

## Requisitos para a próxima prova operacional

1. Determinar, a partir de fluxo autorizado e read-only, como correlacionar uma auditoria concreta (`behavior.unavailable` ou `conversation.blocked`) ao span que a representa, sem atribuir implicitamente a correlação de uma entrada diferente da mesma interação.
2. Comprovar o vínculo temporal e de tenant e a semântica de cada evento. O formato do Zabbix PROBLEM não gera automaticamente um `eventid` individual por decisão.
3. Confirmar retenção e amostragem do Tempo no período solicitado antes de classificar uma ausência de trace. Uma busca vazia nunca se converte em `OBSERVED_HEALTHY`.
4. Somente depois dos vínculos exatos, considerar um **resumo sanitizado e sob demanda** no MGMT, sem execução periódica de buscas de trace, sem sobrecarregar o AGT e sem exibir identificadores.
5. Para correlacionar as camadas OSI L1–L4, é preciso confirmar **o caminho específico de comunicação** e medições do mesmo alvo e janela. Panorama 8/8 de rede permanece apenas contexto geral.
6. Qualquer mudança na instrumentação funcional ou no envelope Zabbix→Relay é uma decisão separada. **Nesta PR nenhum contrato vivo foi alterado.**

## Limites de prova e validação

Os números reais acima correspondem à janela de auditoria e **não** são fixtures de testes; os fixtures do módulo são sintéticos. O código V0 comprova critérios de aceitação/rejeição e saída sanitizada, não certifica a API do Tempo, o fluxo runtime nem uma associação causal já em produção. O bloqueio de acesso remoto deve permanecer registrado e respeitado.

**Princípio de troubleshooting:** vínculo de mensagem ≠ vínculo de falha ≠ causa raiz ≠ autoridade para executar correção. Uma ficha responsável deve declarar cada uma dessas quatro fronteiras.
