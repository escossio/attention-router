# Registro de Falhas Operacionais da Andy (AFO)

**Documento vivo de governança operacional.** Cada AFO possui um arquivo permanente e isolado. Evidência, correção, rollback, regressão, medição e fechamento são registrados na própria AFO.

A degradação operacional já era percebida antes da introdução de VLANs e OpenTelemetry. Esses mecanismos foram adotados como resposta à dificuldade de localizar a falha. A primeira quebra causal comprovada até agora é o Worker encerrado no reboot de 2026-09-21 e não restaurado automaticamente.

## Índice

| AFO | Falha | Estado inicial |
| --- | --- | --- |
| [AFO-2026-001](AFO-2026-001.md) | Generic Worker não retorna após reboot | `ROOT_CAUSE_PROVEN` |
| [AFO-2026-002](AFO-2026-002.md) | Falha de detecção do pipeline degradado | `ROOT_CAUSE_PROVEN` |
| [AFO-2026-003](AFO-2026-003.md) | Cutover de VLAN deixa Worker em Created | `ROOT_CAUSE_PROVEN` |
| [AFO-2026-004](AFO-2026-004.md) | Reconstrução perde paridade de configuração efetiva do Worker | `ROOT_CAUSE_PROVEN` |
| [AFO-2026-005](AFO-2026-005.md) | Corrida entre initialize e recoverConnectedPage no Transport | `VERIFIED` |
| [AFO-2026-006](AFO-2026-006.md) | Puppeteer ESM impede hook de existing-page attach | `VERIFIED` |
| [AFO-2026-007](AFO-2026-007.md) | Collector OTLP não publica loopback quando isolado apenas em rede internal | `VERIFIED` |
| [AFO-2026-008](AFO-2026-008.md) | Blueprint de canário contamina contexto orgânico sem binding | `ROOT_CAUSE_PROVEN / PARKED` |
| [AFO-2026-009](AFO-2026-009.md) | Grupo WhatsApp não recebe classificação canônica de grupo | `ROOT_CAUSE_PROVEN / PARKED` |
| [AFO-2026-010](AFO-2026-010.md) | Identidade de desenvolvimento “Alex” vaza para resposta real | `ROOT_CAUSE_PROVEN / PARKED` |
| [AFO-2026-012](AFO-2026-012.md) | Proxy Grafana perde upgrade Live e encaminha Basic Auth externo | `VERIFIED` |

### Registros descartados

| ID reservado | Motivo | Estado |
| --- | --- | --- |
| [AFO-2026-011](AFO-2026-011.md) | Ausência planejada da VM no notebook do operador; não constitui incidente | `DISCARDED` |

**Versão:** 0.3
**Data:** 2026-09-25
**Estado:** documento vivo de investigação; nenhuma correção de código autorizada por este registro
**Baseline de referência:** último runtime comprovadamente capaz de responder ponta a ponta em 2026-09-20

## Princípio operacional

As falhas reais passam a ser tratadas como laboratório de engenharia. Nenhuma falha é considerada encerrada apenas porque o serviço voltou a funcionar. Cada AFO deve produzir evidência, causa raiz, observabilidade, teste de regressão, procedimento de recuperação e medição antes/depois.

IDs AFO são permanentes. Sintomas não recebem IDs independentes quando pertencem à mesma causa raiz; ficam ligados à AFO causal. Novas evidências podem alterar a interpretação e o status, mas não reutilizam o número.

`DISCARDED` registra uma observação invalidada, fora do ciclo de falhas válidas.
O arquivo permanece como tombstone; seu ID não volta ao conjunto disponível.
Após a reconciliação do AFO-011, AFO-2026-012 foi reservado para a falha de proxy Grafana comprovada independentemente.

Estados permitidos:

`OBSERVED -> INVESTIGATING -> ROOT_CAUSE_PROVEN -> REMEDIATED -> VERIFIED -> CLOSED`

Fechamento exige, no mínimo:

1. causa raiz demonstrada;
2. correção mínima aplicada por caminho controlado;
3. rollback conhecido;
4. teste de regressão;
5. observabilidade capaz de detectar reincidência;
6. canário físico ou E2E quando o efeito é operacional;
7. medição antes/depois;
8. documentação de aprendizado arquitetural.

---

## Linha do tempo consolidada

### 2026-09-20 — último baseline funcional comprovado

Há três fluxos reais no PostgreSQL que chegaram a:

`Decision -> AUTO_ALLOWED -> ExecutionIntent SENT -> Outbox DONE`

Horários locais aproximados: 02:10, 02:22 e 11:30.

O Worker efetivo era `attention-router:idiolect-v1l-6611eac1`, build SHA `6611eac16b2e6cf6f4d7bd1fb2cfdca627d8b5f3`.

O `docker inspect` preservado prova que esse Worker foi criado com uma pilha de 12 Compose files e, entre outros valores, tinha:

- `AGENT_EXECUTION_ENABLED=true`
- `AUTONOMOUS_EXECUTION_ENABLED=true`
- `EXTERNAL_DELIVERY_ENABLED=true`
- `AGENT_DECISION_DEFAULT_BLUEPRINT_ID=d3e3c7f6-d3ee-47f2-bbd8-44d9f0742763`
- `OWNER_CONTROL_SEMANTIC_ENABLED=true`
- `STT_ENABLED=true`

### 2026-09-21 21:45:57 — primeira quebra operacional comprovada

O Worker termina com:

- `ExitCode=137`
- `OOMKilled=false`
- `restart_policy=no`

O horário coincide com o shutdown do AGT. O host volta em 21:46.

O Worker não retorna automaticamente.

### 2026-09-22 a 2026-09-24 — pipeline sem consumidor efetivo

Mensagens passam a apresentar estados incompletos, Grace abertas, filas sem processamento e interações que não chegam ao estágio de decisão/execução esperado.

A ausência de visibilidade clara sobre o ponto de falha motiva a evolução de segmentação de rede, painel de fluxo e OpenTelemetry.

### 2026-09-24 — cutover de VLANs

O Generic Worker já estava parado antes do cutover.

Durante a reconstrução:

- VLAN 213 é recriada;
- o Worker antigo é removido;
- uma tentativa de `docker compose create --no-deps --no-build worker` falha por opção inválida;
- um Compose temporário `/tmp/worker-final-create.yaml` é usado;
- o container é criado, mas permanece em `Created` e não é iniciado;
- a pilha histórica de overrides não é preservada integralmente.

### 2026-09-25 — recuperação parcial

O Worker é recriado com `attention-router:native-otel-b3a0c302` e VLAN 213, volta a `running/healthy`, processa Grace e Queue novamente.

Porém, a configuração atual contém `AGENT_EXECUTION_ENABLED=false`.

Fluxos direct atuais chegam a:

`Decision -> AUTO_ALLOWED -> ExecutionIntent RELEASED -> BLOCKED`

com:

`blocked_reason=AGENT_EXECUTION_DISABLED`

Nenhuma Outbox é criada nesses casos.

### OpenTelemetry/VLANs

A segmentação, o painel e OpenTelemetry foram introduzidos depois que a falha operacional já existia, como mecanismos de visibilidade e isolamento. Não são tratados como causa inicial do apagão da Engine.

O rollout de observabilidade teve falhas próprias, registradas abaixo, mas posteriores à perda original do Worker.

---

# Dependências entre AFOs

- AFO-001 explica a primeira perda comprovada do Generic Worker após reboot.
- AFO-002 explica por que a degradação foi difícil de localizar rapidamente.
- AFO-003 aconteceu durante o esforço posterior de segmentação e manteve o Worker fora.
- AFO-004 explica por que, depois de religado, o Worker atual não é semanticamente equivalente ao último runtime funcional.
- AFO-005, AFO-006 e AFO-007 são falhas encontradas no esforço de observabilidade/Transport; são posteriores à falha original e não devem ser confundidas com sua causa.
- AFO-008, AFO-009 e AFO-010 são falhas de contexto/classificação já demonstradas, mas não explicam por que a Engine deixou de enviar após 20/09. Ficam estacionadas até recuperação do baseline operacional.
- AFO-012 registra duas falhas comprovadas da fronteira Apache/Grafana; não estabelece causalidade para o erro DOM `insertBefore`.
- AFO-011 foi descartada por esclarecimento do operador: a ausência da VM era esperada. Não fundamenta correção, incidente ou mudança de CI; o ID permanece reservado e não será reutilizado.

---

# Baseline de recuperação: “dois passos atrás, dez à frente”

O objetivo não é restaurar cegamente o runtime de 20/09.

Esse runtime respondia, porém carregava defeitos hoje conhecidos, incluindo o default para `Autonomy Canary` e a identidade hardcoded `Alex`.

A estratégia é usar o último runtime funcional como referência comportamental e reconstruir um runtime candidato com:

1. capacidade E2E comprovada do baseline;
2. topologia atual segmentada por VLAN;
3. release atual;
4. configuração efetiva explícita e versionável;
5. nenhuma reintrodução deliberada de contaminações AFO-008/AFO-010;
6. observabilidade suficiente para provar cada estágio.

---

# Plano de instrumentação antes da próxima correção

Nenhuma correção relevante deve ser aplicada sem capturar um baseline “antes”.

## Runtime / configuração

Capturar por serviço:

- container ID;
- image + digest;
- build SHA/runtime head;
- restart policy;
- Compose/config provenance;
- fingerprint da configuração efetiva;
- gates críticos materializados;
- network attachments, IPs e gateways;
- health state;
- start time/restart count.

Não registrar segredos. Valores sensíveis devem ser redigidos antes de persistência em logs/documentação.

## Transport inbound

Registrar de forma segura:

- provider/source;
- source_account;
- classificação de conversa;
- from_me classification;
- message type;
- spool state;
- ingress attempt;
- hash/ID técnico seguro;
- trace_id/span_id;
- duração até Ingress.

## Internal Ingress

- admission status;
- HMAC/auth result;
- idempotency disposition;
- inbound_event_id;
- interaction_id;
- correlation_id;
- conversation classification;
- canonical event result;
- parent/link de tracing.

## Control / Owner Grace

- authority result;
- reason code;
- grace_window_id;
- generation;
- opened_at / due_at / released_at / canceled_at;
- claim worker;
- duração real da Grace.

## Queue

- queue_id;
- kind;
- status;
- created_at;
- claim worker;
- claim age;
- terminal reason.

## Decision

- resolution source do actor;
- resolution source do blueprint (`BINDING`, `EXPLICIT_DEFAULT`, `RANKED_FALLBACK`, `NONE`);
- policy resolution source;
- agent_path_enabled;
- semantic_source;
- decision_type;
- recommended_action;
- confidence;
- context_sufficient;
- missing_information count e classes seguras;
- response_source;
- model/version quando Agent for usado;
- fingerprint de contexto sanitizado.

Raw prompt/contexto sensível não deve ser exportado para observabilidade geral. Se necessário para diagnóstico, usar modo local, temporário, access-controlled e explicitamente autorizado.

## Autonomy

Persistir snapshot dos gates relevantes no momento da decisão:

- autonomous_execution_enabled;
- external_delivery_enabled;
- agent_execution_enabled;
- activation timestamp/freshness;
- actor/audience scope;
- automatic_execution_allowed;
- reason_code.

## Execution

- execution_intent_id;
- authorization_source;
- release_status;
- execution_allowed;
- external_delivery_allowed;
- blocked_reason;
- resultado de recipient resolution;
- transport readiness usada no gate.

## Outbox

- outbox_id;
- action_type;
- destination;
- status;
- available_at;
- attempt_count;
- claim worker;
- last_error class;
- idempotency hash;
- timestamps de criação, claim, dispatch e conclusão.

## Transport outbound

- request attempt;
- idempotency key hash;
- transport readiness;
- HTTP/result class;
- provider message reference presence;
- duração;
- retry/reconciliation disposition.

## E2E

Para cada canário:

`INBOUND -> INGRESS -> CONTROL -> QUEUE -> DECISION -> EXECUTION -> OUTBOX -> OUTBOUND`

Registrar duração por aresta e total.

Estados downstream inexistentes devem aparecer como `NOT_CREATED` ou `NOT_APPLICABLE`, nunca simplesmente `WAITING`.

---

# Protocolo de correção para cada AFO

1. congelar evidência;
2. confirmar último known-good;
3. reproduzir sem mutação sempre que possível;
4. adicionar/validar instrumentação antes da correção;
5. estabelecer métrica baseline;
6. aplicar a menor mudança possível;
7. observar a mesma assinatura causal;
8. executar teste de regressão;
9. executar canário físico quando aplicável;
10. medir antes/depois;
11. validar rollback;
12. atualizar AFO para VERIFIED;
13. somente então avaliar CLOSED.

---

# Próximo passo autorizado

Antes de qualquer mudança de código ou flag, produzir o **Manifesto de Paridade do Generic Worker**:

`Worker funcional 20/09` versus `Worker atual`.

Para cada diferença, classificar:

- `RESTORE_REQUIRED`
- `REPLACED_BY_VLAN`
- `OBSOLETE_BY_DESIGN`
- `LEGACY_CANARY_CONTAMINATION`
- `ACCIDENTAL_DRIFT`
- `NEEDS_PROOF`

Somente depois desse manifesto deve existir uma proposta de runtime candidato para teste.
