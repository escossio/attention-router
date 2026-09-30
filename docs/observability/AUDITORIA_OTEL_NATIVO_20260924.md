# Andy / Attention Router — auditoria OpenTelemetry nativo

Data da auditoria: 2026-09-24.

Estado: **AUDITORIA READ-ONLY CONCLUÍDA; IMPLEMENTAÇÃO NÃO AUTORIZADA.**

Este documento registra a conclusão e o plano entregues na conversa anterior. Sua gravação em arquivo foi autorizada depois da auditoria. As constatações de runtime são o retrato daquela inspeção, não uma nova certificação.

**Recomendação: endurecer a instrumentação Python já existente antes de habilitar a borda Transport → Ingress.**

Na auditoria não houve edição de arquivos, atualização de STATUS.md, instalação, teste, acesso ao banco, migration, reinício, envio de mensagem, commit, push ou PR. Esta etapa posterior realiza apenas a documentação e o registro correspondente no STATUS.md.

Achados principais:

- Já existe instrumentação OpenTelemetry manual parcial no Python, sem exportação configurada nos containers inspecionados.
- O correlation_id é recriado no caminho ordinário de entrada.
- Grace, processamento em lotes e mídia impedem representar corretamente tudo como uma cadeia linear.
- O helper atual pode exportar exceções e campos derivados de conteúdo.
- O acesso dos serviços da aplicação ao Collector não foi demonstrado.
- O Generic Worker permanece parado e usa uma versão anterior à do Ingress.

## A. Arquitetura real encontrada

Referência principal: /srv/projetos/attention-router-releases/5aed0cbf6e832c8f06d88588741f7b22db96f280. Arquivos relevantes foram confrontados com os arquivos presentes nos containers. O checkout /srv/projetos/attention-router, identificado como legado, não foi tratado como fonte do runtime atual.

Nos caminhos relativos Python deste relatório, a base é esse release. A base Transport é /srv/projetos/attention-router-releases/b23960acef73db39f88af03906c34db772095b9f/whatsapp-transport-local. ROC está em /srv/projetos/attention-router-roc-e2e.

| Componente | Implementação real | Situação encontrada |
|---|---|---|
| Transport | JavaScript/CommonJS, Node 22, whatsapp-web.js, Puppeteer | Serviço systemd ativo; fonte b23960ac, com alterações operacionais locais registradas |
| Browser | Processo externo, acessado por Puppeteer/CDP | Fora do processo Node; sem instrumentação OTel própria identificada |
| Internal Ingress | Python 3.12.14, FastAPI/Uvicorn | Container ativo, release 5aed0cbf |
| API | Python 3.12.14, FastAPI/Uvicorn | Container ativo, imagem 6b2aa73; versão distinta do Ingress |
| Ingress Meta | FastAPI; autenticação e callbacks específicos | Implementado; container correspondente parado |
| Ingress neutro | FastAPI, credencial vinculada a tenant, inbox PostgreSQL | Caminho separado de integração; não equivale ao ingresso WhatsApp |
| Control | Módulos Python de Owner Control e controles operacionais | Camada interna, não microsserviço |
| Owner Grace | Máquina de estados persistida no PostgreSQL | Agrega mensagens, altera âncora, libera ou cancela processamento |
| Queue | Tabela PostgreSQL queue, payload JSON | Não há broker externo neste fluxo |
| Generic Worker | Loop Python | Container em Created, release 27d7a559; não está executando |
| Decision | Pipeline Python, regras e agente opcional | Resolução, contexto, proposta e validação |
| Authorization | Autonomy, revisão humana, controles e gates de execução | Camadas distintas, distribuídas pelo código |
| Execution | Intenções, liberação, validação e execução de capabilities | Mais de uma fronteira; intenção não equivale a efeito |
| Outbox | PostgreSQL, claim e finalização separados da chamada externa | Texto e voz convergem para despacho |
| Artifact | Armazenamento local imutável + metadados PostgreSQL | Understanding possui worker especializado ativo |
| STT/TTS | Adaptadores Python → HTTP → TTS API Switcher | Serviço separado, Python 3.12.14/FastAPI, release ea1d3e52 |
| Normalização de áudio | Python → ffmpeg/ffprobe | Fronteira de subprocesso |
| ROC reconstruído | Bridge Python 3.13 + OTel 1.44.0 | Independente, ativo |

Control, Grace, Decision e Authorization não devem receber identidades fictícias de serviços. service.name deve identificar o processo real que executou o span.

## B. Fluxo real da mensagem

~~~mermaid
flowchart TD
    B[Browser / eventos WhatsApp] --> T[Transport Node]
    T --> S[Spool durável]
    S --> I[HTTP Internal Ingress]
    T --> M[Captura e notificação de mídia]
    M --> A[Artifact / STT / Understanding]
    I --> C{Classificação e controle}
    C -->|Owner Command| OC[Control e confirmação na Outbox]
    C -->|Observação de saída| O[Atualização ou cancelamento Grace]
    C -->|Mensagem ordinária| P[Receipt e Interaction]
    P --> G{Grace aplicável}
    G -->|Sim| W[Janela persistida]
    W --> Q[Queue decision]
    G -->|Não| Q
    A --> Q
    Q --> D[Worker / readiness / Decision]
    D --> AU[Autonomy / aprovação / gates]
    AU --> E[Execution Intent]
    E --> X[Liberação e enqueue]
    X -->|Voz| V[TTS e normalização]
    X -->|Texto| OB[Outbox]
    V --> OB
    OC --> OB
    OB --> DS[Claim / revalidação / dispatch]
    DS --> TS[Transport sendMessage]
    TS --> B
~~~

Esse diagrama representa caminhos implementados. **Não certifica que o percurso completo esteja ativo hoje**, pois o Generic Worker está parado.

Existem caminhos laterais de memória, timers, integração, scheduler e contexto pessoal. Eles não devem aparecer como etapas obrigatórias de toda mensagem.

O loop recente executa Grace, STT/Artifact, Decision, enqueue, TTS e Outbox em passagens separadas, com commits intermediários: attention_router/infrastructure/worker.py:325.

Pontos de implementação:

- Transport: src/transport.js:1090, callbacks de mensagem; src/bridge.js:38, normalização/spool; src/spool.js:75, tentativa HTTP e retry.
- Ingress: attention_router/web/internal_ingress_app.py:72, process_internal_event; :269, endpoint e executor.
- Recepção: attention_router/application/services.py:1338 e :1539.
- Grace: attention_router/application/owner_reply_grace.py:213, :425 e :501.
- Decision: attention_router/application/decision_pipeline.py:361.
- Autonomy: attention_router/application/autonomy.py:184.
- Execution: attention_router/application/execution.py:164, :252 e :397.
- Outbox: attention_router/application/services.py:2232 e :2427.
- Artifact: attention_router/application/artifact_understanding.py:389 e :633.
- STT/TTS: attention_router/application/voice_transcription.py:172 e voice_tts.py:63.

## C. Localização e comportamento do correlation_id

| Ponto | Comportamento observado |
|---|---|
| Transport | Não cria nem propaga correlation_id no inbound atual |
| NormalizedInboundEvent | Cria UUID por padrão |
| Adapter interno | Aceita correlation_id opcional |
| Audits iniciais de normalização/binding | Usam o ID do evento normalizado |
| receive_inbound_event | Cria outro UUID, usado no receipt e na interação |
| Replay ordinário | Retorna a correlação persistida da interação existente |
| Observação outbound do Owner | Caminho próprio; preserva o ID do evento normalizado |
| Decision/Intent | Relacionamento principalmente por IDs de evento, interação e decisão |
| Outbox | Recebe a correlação da interação/receipt |
| HTTP outbound | Adaptador Python inclui correlação; Transport não a mantém como contexto de tracing |
| STT/TTS | Serviço cria request_id próprio; não é a correlação Andy |
| Diversos audits | Podem ter correlation_id=NULL, mesmo associados a uma interação |

A recriação está explícita em attention_router/application/services.py:1353. A chamada que recebe o evento normalizado não encaminha seu ID para essa função (:1620).

NormalizedInboundEvent cria o valor padrão em attention_router/adapters/inbound.py:35; seu normalized_payload exclui correlation_id, received_at e tenant_id (:76). O adapter interno aceita o ID em attention_router/adapters/internal_ingress.py:31 e :80.

repository.audit() infere tenant pela interação, mas não infere correlation: attention_router/infrastructure/repository.py:71. Isso explica lacunas nos audits de Decision, Artifact e voz. Chamadas de mídia em voice_media.py:94/:136, voice_transcription.py:253, artifact_understanding.py:491/:769/:800 e voice_tts.py:187/:202 omitem correlation.

**Consequência para o plano:** preservar exatamente a semântica existente. Nos spans da mensagem persistida, roc.correlation_id deve vir do receipt/interação canônicos. Corrigir a recriação seria uma mudança funcional separada.

O teste “correlation intacto” deve provar ausência de regressão, sem afirmar que hoje existe um único ID invariável desde a normalização.

## D. Fronteiras de propagação e escolha da raiz

| Fronteira | Propagação proposta |
|---|---|
| Browser → callback Node | Contexto manual por evento; não instrumentar internamente o WhatsApp Web |
| Callback → Promises | Escopo isolado por mensagem usando contexto assíncrono Node |
| Transport → spool → retry/restart | Carrier lateral opcional, separado dos bytes funcionais |
| Transport → Ingress HTTP | traceparent/tracestate em headers |
| FastAPI → ThreadPoolExecutor | Transferência explícita de contexto, com restauração ao terminar |
| Funções Python síncronas | Contexto corrente, sem parâmetros de negócio adicionais |
| Ingress → Queue | Reaproveitar payload.observability.trace_context |
| Ingress → Grace → Queue | Contexto durável da âncora e Links para mensagens agregadas |
| Decision → Intent → despacho posterior | Novo armazenamento opcional de contexto fora dos campos funcionais |
| Outbox → Transport | Extração por item e injeção em headers HTTP |
| Artifact/STT/TTS | Contexto por trabalho/derivação; extração após autenticação |
| Python → ffmpeg/ffprobe | Span ao redor da chamada; sem SDK no binário |
| Docker/VLAN | Propagação pelos protocolos anteriores; Docker não transporta contexto automaticamente |

O Ingress usa run_in_executor() com pool próprio de 16 threads: attention_router/web/internal_ingress_app.py:34 e :269. Instrumentar apenas FastAPI não resolve essa passagem.

Regras do carrier:

- Somente campos explicitamente permitidos; inicialmente W3C Trace Context.
- Validar formato, tamanho e vínculo com o trabalho/tenant.
- Contexto ausente ou inválido degrada a observabilidade, preservando a execução autorizada.
- Extrair a partir de contexto vazio em consumidores; restaurar o contexto anterior em finally.
- Não usar baggage para tenant, autoridade ou dados pessoais.
- Não propagar automaticamente aos provedores externos.
- Headers de trace não são autenticadores. O HMAC atual cobre timestamp e body, não esses headers.
- Não inserir tracing em payloads sujeitos a hash, assinatura, fingerprint ou idempotência.

Essas regras seguem a separação entre contexto distribuído e identidade de negócio do [W3C Trace Context](https://www.w3.org/TR/trace-context/).

### Raiz recomendada

**Admissão no Internal Ingress.**

Preservar o nome nativo existente attention.message, vinculando-o à correlação canônica assim que ela é criada ou recuperada. Para mensagens admitidas, o root deve terminar com:

    roc.correlation_id = correlação persistida
    roc.synthetic = false

O escopo deve distinguir processamento de admissão e commit. Rejeições anteriores à existência de uma mensagem canônica pertencem ao trace da tentativa de ingresso.

O Transport só conhece a correlação canônica depois da resposta HTTP. Se essa resposta se perder, um root iniciado no Transport pode terminar sem conhecê-la. Não é possível acrescentar o atributo retroativamente a um span já exportado.

Recomendação inicial:

1. Trace da tentativa Transport → HTTP Ingress.
2. Root da mensagem admitida no Ingress, com Span Link para a tentativa.
3. Continuidade por pai/filho da mensagem admitida até o despacho.

Isso permite seguir a operação por relações explícitas, mas **não promete um único trace_id desde o primeiro callback do Browser**. Exigir simultaneamente essa propriedade e correlação obrigatória no root, inclusive diante de resposta perdida, demandaria desenho adicional de protocolo/durabilidade.

Não manter root aberto por horas enquanto há trabalho em spool/Grace. Filhos duráveis podem iniciar depois do término do parent. Em restart, restaurar SpanContext, nunca objeto Span.

## E. Pontos candidatos a spans

Já existem spans em attention_router/observability/tracing.py, services.py, decision_pipeline.py, autonomy.py, execution.py e worker.py.

| Operação | Nome proposto/reaproveitado | Ajuste necessário |
|---|---|---|
| Mensagem admitida | attention.message | Correlação canônica e escopo coerente com admissão |
| Recepção Transport | transport.receive | Um evento efetivamente encaminhado |
| Tentativa HTTP de ingresso | transport.ingress_attempt | Um span por tentativa, sem duplicar mensagem |
| Aceitação | ingress.accept | Autenticação/admissão com resultado explícito |
| Controle | control.evaluate | Operação enumerada: comando, pausa, observação etc. |
| Grace | grace.defer, grace.release, grace.cancel | Transições reais |
| Enqueue | queue.enqueue | Por mensagem, dentro do contexto produtor |
| Claim/processamento | queue.claim, worker.dispatch | Por item; incluir waits/cancelamentos hoje invisíveis |
| Decisão | Spans existentes de actor, policy, agente, decisão e behavior | Corrigir escopos e retirar atributos inseguros |
| Autorização | autonomy.evaluate e authorization.evaluate | Gates concretos, sem duplicar a mesma avaliação |
| Intenção | execution.intent | Já existe; preservar significado |
| Capability | capability.execute | Somente quando realmente chamada |
| Outbox | outbox.enqueue, outbox.dispatch | Por intenção/item, não por lote |
| Envio | transport.send | Separar chamada Python e execução Node |
| Mídia | artifact.stage, artifact.understanding.process, stt.transcribe, tts.synthesize, audio.normalize | Somente etapas alcançadas |

Correções de significado:

1. inbound.receive hoje mede principalmente criação da interação, não todo o ingresso.
2. policy.resolve registra resultados de resolução já realizada; sua duração atual não mede toda a operação.
3. outbox.enqueue envolve lote de até dez intenções e pode juntar mensagens/tenants: attention_router/application/execution.py:396.

Não usar grace.wait como span aberto durante minutos/restarts. Transições nativas e duração de espera como atributo numérico evitam apresentar duração reconstruída como execução observada.

## F. Auto-instrumentação possível e dependências

| Runtime/biblioteca | Possibilidade | Recomendação inicial |
|---|---|---|
| Python FastAPI/ASGI | HTTP server | Seletiva, excluindo health/admin e spans internos redundantes |
| Python SQLAlchemy | Operações de banco | Posterior; sem SQL bruto, parâmetros ou sqlcommenter |
| Python psycopg | Driver PostgreSQL | Escolher esta camada ou SQLAlchemy, evitando duplicação |
| Python urllib.request | Adaptadores outbound/STT/TTS | Injeção e spans manuais são a menor mudança |
| Python HTTPX | Subjacente a chamadas de SDK | Somente depois de certificação de privacidade |
| Python threads | Propagação automática disponível | Preferir passagem explícita no executor conhecido |
| Node http | Servidor outbound | Seletiva por rota |
| Node fetch | Instrumentação Undici | instrumentation-http isoladamente não cobre esse caminho |
| Browser/CDP | Sem cobertura segura identificada | Não instrumentar frames/página WhatsApp nesta migração |

FastAPI oferece exclusões e controles de captura de headers. A instrumentação de threads propaga contexto, mas não cria spans por si. SQLAlchemy pode acrescentar comentários às queries; manter esse recurso desligado.

Fontes primárias consultadas:

- [FastAPI](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html).
- [Threading](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/threading/threading.html).
- [SQLAlchemy](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/sqlalchemy/sqlalchemy.html).
- [Undici/fetch](https://raw.githubusercontent.com/open-telemetry/opentelemetry-js-contrib/main/packages/instrumentation-undici/README.md).
- [Contexto JavaScript](https://opentelemetry.io/docs/languages/js/context/).

Dependências:

- Andy Python: API, SDK e exporter OTLP/HTTP 1.27.0 já presentes. Spans manuais não exigem nova dependência.
- Auto Python futura: instrumentors compatíveis com o conjunto escolhido. Não instalar latest sobre a base existente.
- Node: API, SDK Node, exporter OTLP/HTTP-protobuf, recursos e contexto/W3C compatíveis; HTTP/Undici somente se necessários.
- TTS: adicionar API/SDK/exporter pinados quando essa etapa for autorizada.
- Bridge: manter seu conjunto independente OTel 1.44.0. Não precisa compartilhar versão de SDK com Andy.

Há drift no Transport: manifesto declara Puppeteer 25.11.0, enquanto o node_modules acessado pelo runtime contém 24.38.0. Adicionar OTel exige build isolado reproduzível; npm install no runtime poderia alterar a integração com o Browser.

## G. Instrumentação manual necessária

A instrumentação manual deve representar decisões e transições que bibliotecas HTTP/SQL não conhecem:

- Classificação de Owner Command e observações outbound.
- Aplicação de controles.
- Abertura, extensão, liberação e cancelamento de Grace.
- Enqueue/claim por trabalho.
- Readiness de voz e Artifact.
- Decisão, autonomia, aprovação e revalidação.
- Intenção versus execução efetiva.
- Persistência da Outbox versus tentativa de envio.
- Replay, envio ambíguo e evidência de entrega.
- Reutilização de derivação Artifact.

Grace agrega múltiplas mensagens e muda a âncora. A liberação deve ter um parent principal e Links limitados para as demais mensagens. Uma mensagem humana que cancela a janela também constitui operação distinta.

Uma chamada sendMessage concluída não deve ser automaticamente descrita como entrega ao destinatário.

## H. Riscos e política inicial

| Risco encontrado | Tratamento proposto |
|---|---|
| Exceção completa exportada | Desabilitar captura automática; registrar somente tipo/código permitido |
| Atributos derivados de IA | Excluir objective, reasoning, failure reason livre e equivalentes |
| Hash de conteúdo | Não exportar; hash não elimina cardinalidade nem risco de inferência |
| Contexto entre tenants | Escopo por item, validação de vínculo e detach garantido |
| SDK inicializado preguiçosamente em threads | Inicialização única e protegida no startup |
| Auto e manual duplicados | Um span por operação definida; instrumentors seletivos |
| Polling excessivo | Nenhum span por poll vazio, healthcheck ou frame CDP |
| Collector lento/fora | Exportação em background, fila limitada e descarte controlado |
| Configuração OTel inválida | Fallback no-op; parsing/import de tracing não pode derrubar startup |
| Restart | Persistir SpanContext, nunca objetos Span em memória |
| Efeito duplicado | Preservar claim, commits, idempotência e tratamento de ambiguidade |
| Versões divergentes | Rollout específico por componente; não atualizar Worker implicitamente |

O helper atual chama record_exception(exc) e também usa o registro automático do context manager: attention_router/observability/tracing.py:123 e :167. O filtro por fragmentos de nome não impede campos como attention.agent.objective, attention.decision.reason ou str(exc).

Os 5 segundos configurados no BatchSpanProcessor não configuram o timeout HTTP do exporter atual. O código instalado do exporter usa seu próprio timeout, por padrão 10 segundos. Configurar e testar separadamente.

### Contrato obrigatório de falha aberta

- Nenhuma chamada OTLP síncrona no caminho funcional.
- Nenhum force_flush() por mensagem.
- Nenhum requisito de Collector/Tempo nos healthchecks funcionais.
- Exporter com timeout e fila limitados.
- Erros de instrumentação não substituem nem engolem erros funcionais.
- Shutdown com orçamento limitado.
- Perda de telemetria permitida sob falha; perda de execução por indisponibilidade do Collector, proibida.

O SDK ajuda, mas não substitui esse contrato de aplicação, especialmente na inicialização: [tratamento de erros OpenTelemetry](https://opentelemetry.io/docs/specs/otel/error-handling/).

### Atributos iniciais

| Categoria | Permitidos |
|---|---|
| Recursos | service.name, service.version, ambiente de deployment |
| Correlação | roc.correlation_id canônico |
| Origem | roc.synthetic=false, roc.trace_source=native |
| Operação | roc.stage, roc.operation, roc.result |
| Controle | roc.policy_version, roc.execution_allowed, roc.external_delivery_allowed |
| Trabalho | roc.queue_kind, tentativa, reutilização, modalidade enumerada |
| Diagnóstico | Status HTTP, tipo/código de erro enumerado, continuidade presente/ausente |

Recomenda-se allowlist central, limites de quantidade/comprimento e enums fechados. Proposta inicial: até 32 atributos por span, strings operacionais até 128 caracteres, correlação até o limite existente de 64, eventos e Links limitados.

Não exportar corpo, telefone, nome, e-mail, documento, prompt, resposta de IA, tokens, credentials, payload, filename, URL completa, query string, SQL bruto ou exceção completa.

roc.correlation_id é naturalmente de alta cardinalidade. Deve ser atributo de trace pesquisável, nunca dimensão de métricas, resource por mensagem ou label de service graph.

## I. Plano incremental

Todos os arquivos abaixo são alterações futuras. Caminhos Python são relativos ao release Andy; caminhos Node são relativos a whatsapp-transport-local/.

### Pré-etapa — tornar a base existente segura, ainda desligada

- **Arquivos:** attention_router/observability/tracing.py, configuração OTel em attention_router/config.py, tests/test_tracing.py.
- **Dependências/spans:** nenhuma dependência nova; nenhum novo estágio.
- **Atributos/contexto:** allowlist, exceções sanitizadas, fallback no-op, extração segura e inicialização única.
- **Testes/PASS:** erro de exporter, configuração inválida, criação/finalização de span com falha; sentinelas privadas ausentes de atributos, eventos, recursos e status; exceção funcional original preservada.
- **Impacto/risco:** baixo com tracing desligado; risco principal é alterar semântica de exceção.
- **Rollback/restart:** reversão do pequeno patch; nenhum restart para preparar/certificar offline. Deploy futuro exige reiniciar somente processos que recebam o código.

### Etapa 1 — borda Transport → Ingress

Dividir em 1A, raiz/admissão no Ingress, e 1B, tentativa HTTP no Transport com Link.

- **Arquivos:** web/internal_ingress_app.py, pontos de tracing em application/services.py; novos helpers Node; src/index.js, src/bridge.js, src/spool.js, configuração, package.json e lockfile. Testes de tracing, ingresso, HMAC e spool.
- **Dependências:** Python reutiliza as atuais; Node recebe o conjunto OTel mínimo pinado.
- **Spans/atributos:** attention.message, ingress.accept, transport.receive, transport.ingress_attempt; atributos mínimos da política acima.
- **Propagação:** headers W3C, contexto explícito no executor e Link entre tentativa e mensagem canônica. Não alterar body/HMAC.
- **Testes/PASS:** admissão, replay, rejeição, concorrência e resposta perdida; correlação canônica no root admitido; relação HTTP correta; Collector fora sem diferença funcional.
- **Impacto/risco:** baixo volume de spans; risco moderado no bootstrap Node e executor.
- **Rollback/restart:** flags desligadas e artefatos anteriores; rollout futuro apenas Transport/Ingress. Browser e Generic Worker permanecem no estado anterior.

Pré-requisito de exportação separado: o Collector atual recebe OTLP, mas seu Compose não publica porta e o conecta à rede interna ROC. Preparar acesso restrito pelos componentes autorizados, preservando segmentação. Isso pode exigir alteração futura em attention-router-roc-e2e/compose.yaml e configuração de rede; não presumir que o hostname atual funcione nas VLANs da aplicação.

PASS dessa parte exige ingestão e consulta de um trace canário. Não basta verificar porta aberta.

### Etapa 2 — contexto durável nas primeiras fronteiras

- **Arquivos:** src/spool.js, helper lateral Node, src/media.js, application/services.py, helper Python; infrastructure/models.py e migration aditiva proposta para contexto de inbound, se necessária para recuperação/replay.
- **Dependências:** nenhuma adicional.
- **Spans/atributos:** queue.enqueue, tentativas de retry; roc.queue_kind, resultado e continuidade.
- **Propagação:** reaproveitar metadata da Queue. No spool, envelope lateral versionado, validado por tenant e hash dos bytes; nunca dentro do body.
- **Testes/PASS:** restart/retry com carrier válido conserva contexto; carrier ausente/corrupto não impede entrega; bytes, hash e idempotência invariáveis.
- **Impacto/risco:** escrita/leitura adicional pequena; risco moderado de associação errada.
- **Rollback/restart:** ignorar metadata lateral e manter schema aditivo; restart futuro dos produtores/consumidores alterados.

Envelope lateral proposto: version, traceparent, tracestate, body_sha256, tenant_scope_hash, created_at e canonical_correlation_id quando conhecido. Armazenar em diretório separado do pending varrido pelo drainer, com limites de tamanho, quantidade e TTL. Limpeza best-effort.

Se gravar o carrier falhar, preservar execução pode implicar perder continuidade. Nesse cenário, o resultado correto é PASS funcional e telemetria incompleta, não continuidade perfeita.

### Etapa 3 — Control / Grace

- **Arquivos:** handlers em services.py, owner_control.py, owner_operational_control.py, owner_reply_grace.py; armazenamento opcional de contexto por janela/membership e migration correspondente quando necessário.
- **Dependências:** nenhuma.
- **Spans/atributos:** control.evaluate, grace.defer/release/cancel; operação, resultado, geração e versão de política.
- **Propagação:** contexto da âncora; Links para memberships e cancelamento humano. Hoje a release cria Queue sem carrier: application/owner_reply_grace.py:473.
- **Testes/PASS:** deadlines, âncora, geração, coalescing, replay e cancelamento idênticos com tracing ligado/desligado; concorrência PostgreSQL certificada nos workers.
- **Impacto/risco:** spans por transição; risco moderado por múltiplas causas.
- **Rollback/restart:** desligar spans/contexto e preservar metadata; reiniciar futuramente somente processos alterados. Não iniciar o Generic Worker para testar produção.

### Etapa 4 — Queue / Worker / Decision e entrada multimodal

- **Arquivos:** infrastructure/worker.py, application/decision_pipeline.py, autonomy.py, artifact_understanding.py, voice_transcription.py, modelos de derivação/contexto e runner especializado.
- **Dependências:** nenhuma nova para spans manuais Andy; OTel no serviço STT caso incluído nessa subetapa.
- **Spans/atributos:** claim e dispatch por item, spans existentes de decisão, readiness, Artifact e STT; modalidade, reutilização, tentativa e resultado.
- **Propagação:** restaurar contexto antes das verificações; persistir nas transições duráveis. Derivação reutilizada recebe span da mensagem atual, sem herdar contexto de outra mensagem.
- **Testes/PASS:** dois tenants no mesmo lote, waits/cancelamentos, timeout, retry/reclaim e reuso; nenhum contexto residual; nenhum conteúdo de IA ou mídia exportado.
- **Impacto/risco:** moderado; risco de spans por polling e timeout de provider.
- **Rollback/restart:** flags e imagem anterior; schema opcional mantido. Restart futuro apenas dos workers/serviço de voz efetivamente incluídos.

O runner especializado observado é /usr/local/lib/attention-router-artifact-understanding/worker.py. A imagem antiga do Generic Worker não deve ser trocada pela recente como efeito colateral desta etapa.

### Etapa 5 — Authorization / Execution / Outbox / saída

- **Arquivos:** autonomy.py, execution.py, services.py, application/platform/execution.py, adaptadores outbound/TTS, voice_tts.py, audio_normalizer.py; modelos de Decision/Intent/Outbox/derivação; Node server.js, outbound-provenance.js, transport.js; TTS app/main.py, helper e dependências.
- **Dependências:** OTel pinado no TTS; restantes reutilizam a base.
- **Spans/atributos:** gates reais, execution.intent, outbox.enqueue/dispatch, transport.send, TTS e normalização; permissões, resultado, tentativa e modalidade.
- **Propagação:** contexto durável por intenção/outbox, extração por item, W3C no HTTP interno. Separar contexto de fingerprints de autorização e execução.
- **Testes/PASS:** preservar commit → rede → finalização; mesmo número de efeitos; replay não envia novamente; ambiguidade mantém tratamento atual; texto/voz consultáveis.
- **Impacto/risco:** maior criticidade, por estar próximo do efeito externo.
- **Rollback/restart:** componente por componente, flags/artefatos anteriores; nenhuma reversão de mensagens ou downgrade destrutivo.

### Etapa 6 — comparação nativo versus reconstruído

- **Arquivos:** novo comparador/relatório offline; configuração/painéis Grafana adicionais e documentação ROC. Bridge preservado.
- **Dependências:** nenhuma obrigatória na aplicação.
- **Spans/atributos:** nenhum estágio funcional novo; identificação de origem e cobertura.
- **Propagação:** nenhuma dependência funcional entre nativo e bridge.
- **Testes/PASS:** localizar ambos os traces, demonstrar uma concordância e uma divergência controlada, distinguir ausência de cobertura e erro real.
- **Impacto/risco:** fora do caminho funcional; risco de comparação incorreta.
- **Rollback/restart:** retirar painel/comparador adicional; nenhum restart Andy. Provisionamento Grafana tratado separadamente.

Nenhuma etapa inclui Path Analyzer ou EXPECTED PATH.

## J. Matriz de certificação

**Nenhum destes testes foi executado nesta auditoria.**

| Nº | Prova | Evidência necessária para PASS |
|---:|---|---|
| 1 | Collector indisponível | Mesmo resultado funcional e efeitos; exporter recusado, lento e fila saturada |
| 2 | Tempo indisponível | Aplicação continua; backpressure permanece na observabilidade |
| 3 | Correlação preservada | IDs e relações iguais ao comportamento basal, incluindo replay e caminhos especiais |
| 4 | trace_id correto | W3C válido; continuidade nas fronteiras acordadas; separações por Link explícitas |
| 5 | Pais/filhos corretos | Grafo assertado por IDs; Grace com Links; ausência de mistura entre itens |
| 6 | Ausência de conteúdo sensível | Sentinelas ausentes de todo OTLP: recursos, atributos, eventos, status e tracestate |
| 7 | Cardinalidade controlada | Nomes/enums fixos; IDs fora de métricas; volume por operação limitado |
| 8 | Sem mudança funcional | Mesmos estados, decisões, deadlines, hashes, idempotência e efeitos |
| 9 | Bridge preservado | Continua produzindo reconstrução independente |
| 10 | Native no Tempo | Trace recuperado pelo ID após exportação |
| 11 | Consulta Grafana | Trace aberto pelo datasource roc-tempo; filtros distinguem origens |
| 12 | Operação ponta a ponta | Ingress → saída contínuo; pré-admissão navegável por Link, com limite explicitado |
| 13 | Erro funcional | Span da operação falha com ERROR e código sanitizado |
| 14 | Operação normal | Sucesso, replay e bloqueio legítimo por política sem ERROR indevido |
| 15 | Restart | Novo tráfego funciona; trabalho persistido conserva contexto quando carrier existe |
| 16 | Componentes não instrumentados | Mensagens/jobs legados continuam funcionando; cobertura parcial identificada |

Acrescentar: dois tenants concorrentes, resposta perdida após commit, crash entre claim/envio/finalização, retry de mídia, reutilização Artifact e worker antigo lendo metadata opcional.

Os testes existentes dão base, mas não certificam E2E: parte de tests/test_tracing.py:55 fabrica a árvore manualmente; o teste de exporter falho não cobre timeout, fila cheia e reinício.

Arquivos existentes úteis para extensão:

- tests/test_tracing.py, test_internal_ingress.py e integration/test_postgres_internal_ingress.py.
- tests/test_owner_reply_grace.py, test_owner_reply_grace_control.py e integration/test_postgres_owner_reply_grace_concurrency.py.
- tests/test_decision_pipeline.py, test_autonomy.py, test_execution.py, test_wwebjs_outbound.py.
- tests/test_artifact_understanding.py, test_whatsapp_voice_flow.py e testes PostgreSQL correspondentes.
- Transport test/hmac.test.js, spool.test.js, ingress-durability.test.js, server.test.js, outbound-provenance.test.js e self-chat.test.js.

Para desempenho, comparar canários sintéticos com tracing ligado/desligado: mesma taxa de sucesso, nenhuma alteração de deadlines e orçamento inicial de aumento de p95 de até max(5 ms, 5% do baseline), com CPU/memória estabilizados. É critério proposto, não resultado medido.

Testes curtos e direcionados podem rodar no AGT. PostgreSQL, migrations, carga e suítes completas devem ir aos workers via `andy-ci-distributed <sha> postgres` ou equivalente. Como publicação de SHA não está autorizada agora, nenhum desses gates deve ser iniciado nesta fase.

## K. Coexistência, comparação, observed path e rollback

O bridge atual já cria trace_id pelo SDK, independentemente de correlation_id: trace-bridge/bridge.py:47 e :476.

| Aspecto | Nativo proposto | Reconstruído atual |
|---|---|---|
| Fonte | Execução observada | Estado/evidência PostgreSQL |
| Identidade OTel | Própria | Própria, inclusive entre snapshots |
| Serviço | Processo real Andy | andy.roc.reconstruction |
| Origem | roc.synthetic=false | roc.synthetic=true |
| Relações | Causalidade real e Links | Cadeia de pais construída |
| Tempos | Operações executadas | Timestamps persistidos/inferidos |
| Falha antes de commit | Pode aparecer | Pode não deixar evidência |
| Artifact/STT/TTS | Cobertura planejada | Não consultados diretamente pelo bridge |

Limites relevantes do bridge:

- Consulta as últimas 50 entradas, com polling de 5 segundos.
- Usa a última queue/decision/intent/outbox encontrada.
- Pode exportar snapshot stalled e terminal como traces distintos.
- Alguns joins e a deduplicação usam apenas correlation, sem tenant.
- Audits com correlation ausente podem ficar fora.
- BLOCKED pode virar ERROR na reconstrução, embora seja resultado normal no nativo.
- O painel atual filtra somente andy.roc.reconstruction.

Comparação futura deve usar tenant validado + correlação + identidade do inbound, considerando tentativas, snapshots e cobertura. Colisões ou vínculo ambíguo devem produzir resultado inconclusivo. **Ausência no bridge não prova falha da instrumentação nativa.**

Não corrigir esses limites do bridge junto da primeira instrumentação. Ele permanece como auditor independente, com suas limitações documentadas.

Collector: otel/collector.yaml recebe OTLP HTTP/gRPC, usa memory_limiter e batch, e encaminha ao Tempo por gRPC. Não há filtro que restrinja roc.synthetic ou service.name. O datasource existente, grafana/provisioning/datasources/tempo.yaml, pode consultar ambas as origens. Painéis nativos devem ser adicionados sem remover a visão reconstruída.

### roc.observed_path

roc.observed_path já existe na reconstrução, mas é derivado de ordem fixa de estágios: trace-bridge/bridge.py:402. O futuro equivalente nativo deve:

- Usar somente spans de negócio realmente observados.
- Respeitar relações causais, retries, ramificações e Links.
- Distinguir caminho parcial de concluído.
- Não converter ausência de span em ausência de execução.
- Não usar spans SQL/HTTP como prova automática de etapa de negócio.

Um root encerrado na admissão não pode receber posteriormente o caminho completo por simples atualização. Esse resumo precisará de processamento posterior, fora desta etapa.

**Não implementar Path Analyzer nem implementar/calcular EXPECTED PATH.**

### Rollback

- Feature flags desligadas por componente.
- Retorno ao artefato anterior, preservado antes de cada rollout.
- Metadata opcional ignorada pelas versões anteriores.
- Schema aditivo mantido; sem downgrade destrutivo.
- Nenhuma alteração ou limpeza de filas, correlações, evidências ou mensagens para desfazer tracing.
- Collector, Tempo e bridge continuam independentes da disponibilidade funcional Andy.

## L. Primeira alteração mínima recomendada

**Preparar somente o endurecimento de attention_router/observability/tracing.py, com os testes correspondentes, mantendo exportação desligada.**

O primeiro patch deve limitar-se a:

1. Allowlist de atributos e recursos.
2. Remoção de exceções completas e captura duplicada.
3. Fallback no-op sem alterar a exceção funcional.
4. Contexto inválido sem herdar o item anterior.
5. Timeout do exporter separado do timeout de flush.

Apenas após esse PASS faz sentido preparar a raiz canônica no Ingress e a borda HTTP com o Transport.

**Nenhuma implementação realizada. Auditoria e plano aguardam revisão e autorização específica para modificações de código, configuração ou runtime.**
