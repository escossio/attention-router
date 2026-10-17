# Zabbix — perfil de evidências em camadas (V0 / projeto)

**Estado:** planejamento, sem instalação no Zabbix e sem mudança no contrato Relay. Este documento complementa o [mapa MGMT](mgmt-observability-context-v0.md) e o [catálogo de falhas](../operations/operational-failure-catalog-v0.md).

## Objetivo técnico

A emissão de um alarme **não é um diagnóstico de causa**. Nosso objetivo é que, para cada código `AR-XXXX`, o operador veja o sintoma, os domínios que realmente foram medidos, os domínios sem evidência, o ponto no fluxo onde a observação se interrompe e a ordem sugerida de verificação. Isso **não é um novo engine**; é catalogação de fatos e suas lacunas usando itens, tags, dashboards, traces e coletores existentes.

Não obrigamos o Zabbix a fazer um comando em cada uma das sete camadas OSI. O modelo OSI é referência de dependência/ordem de troubleshoot; execução seletiva segue caminho real do fluxo observado. Aplicação e PostgreSQL são L7 na referência OSI, mas aparecem como **domínios operacionais próprios**, mais úteis ao operador.

## Esquema e estados — sem conclusão indevida

Esquema proposto: `ops/observability/failure-catalog/layered-evidence.v0.schema.json`. Domínios e fontes: `layered-dimensions.v0.json` e `evidence-sources.v0.json`.

| Estado | Significado | Exigência |
| --- | --- | --- |
| `OBSERVED_HEALTHY` | Verificação relevante passou **na janela e alvo efetivo** | Fonte válida, timestamp, scope e referência |
| `OBSERVED_FAILURE` | Verificação concreta falhou | Mesmo conjunto de evidências; não implica causa raiz |
| `NO_RELATED_ANOMALY` | Fonte consultada, **sem anomalia relacionada** aos campos testados | Delimitar métrica, alvo e janela; não equivale a “camada perfeita” |
| `UNKNOWN` | Fonte falhou, dado expirou ou conclusão não é confiável | `reason_code` explícito; não presumir resultado |
| `NOT_OBSERVED` | Fonte não foi consultada / não existe coleta naquele domínio | `basis=NONE`, nenhuma referência inventada |
| `NOT_APPLICABLE` | Evidência de que domínio não participa do caminho em questão | Justificativa no contrato do detector, sem inventar saúde |

Se o OpenTelemetry não mostra uma falha, **não** registrar automaticamente `NO_RELATED_ANOMALY`. Sem trace aplicável, marca-se `NOT_OBSERVED`; se o Collector/Tempo estiver indisponível ou o dado vencido, `UNKNOWN`. Um span saudável de outro serviço não comprova saúde da operação afetada.

Todo registro tem `source_event_ref` opaco de evento, código semântico separado, período observado, `observed_at`, fonte(s), cobertura (`OBSERVED_PATH`, `GENERAL_HEALTH`, `PARTIAL`, `NONE`), método (`DIRECT_MEASUREMENT`, `DERIVED_WITH_CONTRACT`, `CORRELATED_MEASUREMENTS`, `NONE`) e no máximo seis referências curtas. Um código ou tag nunca deve conter payload confidencial, log livre ou interpretação causal.

## Ordem sugerida para o operador

1. **Integridade da coleta:** a sonda estava funcionando e o dado é novo?
2. **Caminho relevante:** qual serviço, evento e intervalo falharam? Qual fluxo deve existir?
3. **Camadas L1–L3:** confirmar condicionais quando a natureza do alarme envolver link/VLAN/IP; preservar as dependências Zabbix OSI atuais. Se a aplicação responde pelo mesmo caminho, registrar evidência disso, não “rede toda OK”.
4. **L4/HTTP:** existe evidência de conexão TCP, handshake ou resposta HTTP? Diferenciar falha de transporte de erro de aplicação.
5. **Runtime/OTel:** processos, health, telemetria, spans visíveis e lacunas de instrumentação por fluxo.
6. **Função da Andy:** entrada esperada, processamento, decisão autorizada, outbox, envio e ACK.
7. **Persistência/entrega:** só declarar perda após comprovar ausência de registro durável e janela aplicável.

O painel de alarme deve mostrar a primeira anomalia observada e uma sequência de verificações **condicionais**, mas não atribuir culpa a uma camada. `NO_RELATED_ANOMALY` indica apenas que a verificação delimitada não mostrou impacto.

## Exemplo AR-1001 — Andy sem resposta

Detector já presente no ROC Zabbix: `ANDY_WA_AUTO_REPLY_STALLED`. Coletor auxiliar: `ANDY_WA_REPLY_OBSERVABILITY_LOST`. Os itens nativos já existentes incluem `andy.reply.functional_stalled` e `andy.reply.probe_ok`. O histórico recente do Zabbix registra PROBLEM duradouro e a sonda disponível; isso não identifica isoladamente qual estágio deixou de responder.

**Primeiro recorte de enriquecimento:** janela e elegibilidade de mensagens; contagem recebida e processada; decisões por tipo/status; expectativa autorizada de resposta; outbox produzida/confirmada; frescor da observação; cobertura e referências OTel quando correlacionáveis. Não basta saber que Docker e API estão HEALTHY.

O planejamento Zabbix para esta falha está em `ops/observability/failure-catalog/zabbix-plan.ar-1001.v0.json`. Seus itens adicionais são **candidatos**, não itens instalados, e precisam de descoberta da fonte mais barata e de contrato comprovado por campo.

## No Zabbix primeiro; Relay depois

1. Mapear a cada código as fontes e atributos já medidos; registrar custo/frescor/retenção por fonte.
2. Onde faltar informação, definir *novos itens ou dependências somente se for necessário*; preferir agregados allowlisted e derivados read-only já disponíveis. Não aumentar carga sem medição.
3. Usar tags `fault_code`, `evidence_profile` em **novos eventos após homologação**; preservar `ops_alert` e correlação de PROBLEM/RECOVERY. Tags de evento não são local para blob de métricas, URLs sensíveis ou traces brutos. Zabbix herda tags ao criar um novo evento; problemas antigos não recebem automaticamente tags retrospectivas.
4. Testar casos sintéticos: cada fonte saudável/falha/stale/ausente, timeout, perda de telemetria, recovery e supressão L1/L2/L3. Não simular um alerta novo de produção só para testar rota.
5. Produzir um **relatório Zabbix de evidência tipada** e verificar sua legibilidade no MGMT; quando necessário, usar referências privadas e curto resumo de status.
6. Somente após dados disponíveis e prova, desenhar **nova versão do contrato Zabbix→Relay** que aceite tipagem e campos adicionais sem quebrar o envelope estrito `occurrence-relay-v0-draft`. Essa nova versão deverá ter replay/idempotência/compatibilidade por origem e ACK comprovados.
7. Não modificar automaticamente o Relay nem MAESTRO ao editar o contrato de origem.

### tcpdump/pcap: sob demanda, não embutido em todo alerta

Uma coleta com `tcpdump` pode registrar pacotes e conteúdo sensível. Necessita alvo e período limitados, minimização, aprovação, retenção privada e comando autorizável. O perfil registra `NOT_OBSERVED` até existir captura autorizada. Não agendar pcap contínuo nem enviá-lo ao Telegram, Zabbix event tag ou ao LLM.

## Referências oficiais para interoperabilidade

- Zabbix 7.4 Event/Trigger tags: https://www.zabbix.com/documentation/7.4/en/manual/config/tagging
- Zabbix correlação de problema e recuperação: https://www.zabbix.com/documentation/7.4/en/manual/config/event_correlation/trigger
- OpenTelemetry SemConv/EventRecord: https://opentelemetry.io/docs/specs/semconv/general/events/
- CloudEvents (identidade source+id, time, subject): https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md
- OSI é referência conceitual, não afirmação de que todas as camadas estão instrumentadas.

**Conclusão desta fase:** documentação e planejamento; sem coleta Zabbix nova, sem deploy MGMT, sem captura pcap, sem webhook modificado, sem investigação autônoma.
