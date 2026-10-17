# Catálogo operacional de falhas — V0 (proposta incremental)

**Data:** 2026-10-17. **Estado:** DRAFT, somente documentação e validação offline. Sem nova ação, media, trigger ou deploy Zabbix.

## Complemento obrigatório — evidências do MGMT e ordem OSI

Este catálogo identifica **a classe do sintoma**, não substitui seu conjunto de evidências. Ver o [mapa MGMT](../architecture/mgmt-observability-context-v0.md) e o [perfil Zabbix de evidências por camadas](../architecture/zabbix-layered-evidence-v0.md), com os registros versionados ops/observability/failure-catalog/layered-*, evidence-sources.v0.json e zabbix-plan.ar-1001.v0.json. Esses documentos já distinguem informações realmente observadas, ausência de observação, observabilidade degradada e verificações sem anomalia relacionada.

**Não emitir novos campos no Relay V0**: a enumeração de domínios/qualidade de coleta é apenas uma proposta de contrato de origem Zabbix até existir versão de transporte compatível e certificada. Uma captura tcpdump/PCAP permanece sob demanda e não é pré-requisito universal para alarme.

## Por que existe

Zabbix observa e classifica **sintomas**; Occurrence Relay registra e transporta sem perder/reordenar identidade; MAESTRO recebe e confirma. A causa e a solução não são escolhidas pelo Zabbix, pelo Relay ou pelo código da ocorrência. O catálogo torna o sintoma identificável, legível e verificável antes de qualquer investigação.

Há códigos existentes que devemos **reutilizar**, não apagar: ANDY_WA_AUTO_REPLY_STALLED, ANDY_WA_REPLY_OBSERVABILITY_LOST, GMAIL_PRODUCT_REFRESH_FAILED, L1_PARENT_DOWN, L2_DOMAIN_DOWN, L3_NEIGHBOR_INCOMPLETE e L3_ENDPOINT_UNREACHABLE. A codificação AR-XXXX é uma **chave de catálogo proposta**, associada a essas identificações de origem, não um novo status HTTP.

## Convenção de numeração

- AR-1xxx: resposta/fluxos de aplicação;
- AR-2xxx: processos e sincronização;
- AR-3xxx: transporte, fila e entrega;
- AR-4xxx: banco e persistência (reservado);
- AR-5xxx: rede e conectividade;
- AR-6xxx: observabilidade e qualidade da coleta;
- AR-7xxx a AR-9xxx: reservados, sem códigos atribuídos.

Um **código** identifica uma classe estável de falha; o ID do evento do Zabbix identifica uma ocorrência concreta; o ID do trigger identifica uma regra nativa que pode variar por instalação; severity representa impacto; status indica PROBLEM/RECOVERY/UPDATE. **Nunca confundir código AR-1001 com HTTP 200/404, Zabbix eventid, código de causa ou resposta ACK do Relay.**

O arquivo machine-readable em ops/observability/failure-catalog/catalog.v0.json contém a legenda e as evidências mínimas por código. Os dois códigos marcados LIVE_TRIGGER têm detector existente; os PROPOSED ainda não possuem detector aprovado; IN_REPO_TEMPLATE é uma definição existente no repositório cuja implantação específica não é atestada por esse catálogo.

## Primeira falha: Andy recebe mensagens e não responde

O Zabbix já possui o trigger ANDY_WA_AUTO_REPLY_STALLED; observamos um evento de problema aberto desde 15 de outubro. A sondagem continuava recebendo dados, mas o problema não mudou para OK. Há registro de **um** envio Telegram naquele início; isso não comprova leitura pelo operador nem notificação atual. A ação permanente para MAESTRO não possui alertas atribuídos a esse evento antigo. Não simular nova falha nem reinserir um evento para gerar notificação.

O trigger mede um estado agregado. Ainda faltam fatos estruturados que permitam localizar **onde** o fluxo parou:

| Grupo | Evidência mínima a coletar | Sem atribuir causa |
| --- | --- | --- |
| Tempo | ocorreu_em, observado_em, janela, frescor da sonda | amostra antiga não é estado atual |
| Entrada | entradas elegíveis, entradas processadas | recebimento não garante resposta devida |
| Decisão | quantidades por tipo/status e razão tipada | DRY_RUN isolado não é falha |
| Saída | enfileiradas, enviadas e confirmadas | outbox vazia não garante sucesso |
| Expectativa | política vigente, elegibilidade e grace window | silêncio autorizado não deve alertar |
| Correlação | source_event_id, trace/interaction ref sanitizados | associação temporal não prova causalidade |

Não enviar corpos de mensagens, dados pessoais, segredos, bearer, stacktrace bruto, payload completo ou hipótese de causa como fato. Na ausência de observação, transmitir UNKNOWN/NOT_COLLECTED, nunca zero fictício.

## Relação com o contrato atual

Hoje o webhook Zabbix em ops/occurrence-relay/zabbix-webhook.js envia um signal estrito:
schema_version, event_id, occurred_at, asset_ref, signal_code, status;
opcionalmente severity, summary, problem_event_id. O sinal de origem usa signal_code ZBX_TRIGGER:<id>. O MAESTRO aceita exatamente o envelope occurrence-relay-v0-draft. **Nenhum campo adicional deste catálogo pode ser enviado ao runtime atual**: seria rejeitado pelo contrato, e um deploy sem migração de contrato causaria perda de entrega.

Passo compatível para V0: manter ZBX_TRIGGER:<id> e IDs originais, mapear códigos no catálogo documental e, após homologação, agregar a trigger tag `fault_code=AR-1001` ou publicar código no título da regra sem violar o webhook. Para transmitir métricas e evidências tipadas, desenhar um **contrato versionado V1** em outra PR, com limites, schema/SDK/testes, evolução aditiva e prova positiva de PROBLEM/RECOVERY/replay antes do rollout. Não reinterpretar summary (texto livre) como estrutura confiável.

O Telegram é só um canal de notificação; **não** é a origem da verdade. A rotina diária deve consultar alertas abertos, novos e recuperados, inclusive os que não emitiram notificação recente. Considerar lembrete de problema persistente com janela de repetição moderada, sem repetir ingestão no Relay nem alterar os event IDs.

## Padrões úteis (não substituem o contrato atual)

- **Zabbix 7.4:** trigger tags e macros EVENT.ID, EVENT.SEVERITY, EVENT.TAGS permitem reconhecer regra e ocorrência. Fonte: https://www.zabbix.com/documentation/current/en/manual/config/tagging
- **CloudEvents 1.0:** referência futura para id, source, type, subject e time; não converter o transporte atual silenciosamente. Fonte: https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md
- **OpenTelemetry Semantic Conventions:** events tipados, occurred time, error.type, atributos limitados e evidência; a classificação depende do contexto. Fonte: https://opentelemetry.io/docs/specs/semconv/general/events/
- **RFC 9457:** problemas tipados em **respostas de APIs HTTP**, não um formato universal de alarmes Zabbix. Fonte: https://www.rfc-editor.org/rfc/rfc9457.html
- **RFC 5424:** severidade Syslog; não substituir a escala Zabbix 0–5. Fonte: https://www.rfc-editor.org/rfc/rfc5424.html

## Rotina operacional de evolução (não automática nesta PR)

1. **Detectar lacuna concreta:** exemplo, Andy segue sem responder mas Telegram não apresenta evento recente. Identificar se é ausência de trigger, trigger persistente, amostra stale, ação sem cobertura, ou ausência de campo no sinal.
2. **Capturar apenas dados observáveis**, com proveniência, janela, estado de coleta e identidade de evento; localizar qual evidência está faltando.
3. **Reutilizar código existente** antes de alocar novo número. Caso novo, registrar código AR-XXXX, legenda em PT-BR, condição testável, exclusões de falso positivo, evidências e estado do detector.
4. **Validar negativo e recuperação** com fixtures sintéticas. Testar decisão no estado UNKNOWN, duplicata e evento de recuperação com vínculo ao problema.
5. **Promover em fatias:** catálogo -> itens/trigger -> action Telegram -> compatibilidade Relay -> MAESTRO. Homologar cada fronteira sem interferir nas outras.
6. **Revisar no cotidiano** ocorrências repetidas com evidência suficiente ou incompleta; ampliar o contrato apenas diante de lacunas demonstradas.

Critério de conclusão de um código: detector reproduzível, evento com proveniência e campos obrigatórios preenchidos, estados PROBLEM/RECOVERY coerentes, supressão de falsos positivos conhecida, entrega idempotente observada e legenda publicada. Não declarar que o catálogo cobre todo o projeto nesta V0.

**Limites:** não criar investigação inteligente, inferir soluções, mexer nas regras de firewall, alterar o MAESTRO standalone ou ativar triggers nesta PR.
