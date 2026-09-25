# Live Flow tracing schema

## Configuração

- `OTEL_TRACING_ENABLED=false` — padrão; no-op local independente de um provider global.
- `OTEL_SERVICE_NAME=attention-router-api` — identidade operacional do processo, validada pela allowlist.
- `OTEL_SERVICE_VERSION=0.1.0` — versão numérica `major.minor.patch` (até quatro dígitos por componente).
- `OTEL_EXPORTER_OTLP_ENDPOINT` — endpoint HTTP OTLP opcional; testes não fazem chamadas reais.
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` — único protocolo suportado neste gate.
- `OTEL_RESOURCE_ATTRIBUTES` — somente overrides de `service.name`, `service.version` e `deployment.environment`, com os mesmos validadores dos defaults.
- `OTEL_TRACES_SAMPLER=parentbased_traceidratio` e `OTEL_TRACES_SAMPLER_ARG=1.0` — razão finita entre 0 e 1; também são aceitos `always_on` e `always_off`.
- `OTEL_BATCH_MAX_QUEUE_SIZE=2048` — 1..8192 spans.
- `OTEL_BATCH_MAX_EXPORT_BATCH_SIZE=512` — 1..1024 spans, nunca maior que a fila.
- `OTEL_BATCH_SCHEDULE_DELAY_MILLIS=500` — 10..60000 ms.
- `OTEL_EXPORT_TIMEOUT_MILLIS=5000` — timeout HTTP do exporter, 1..10000 ms; convertido em segundos no construtor OTLP.
- `OTEL_FLUSH_TIMEOUT_MILLIS=1000` — orçamento de espera de `flush_tracing()`, 0..10000 ms. Um argumento explícito, inclusive zero, substitui esse default.

O provider é local, inicializado uma única vez sob lock. Falha de configuração,
SDK, provider, processor ou exporter degrada a telemetria para no-op. Valores OTel
malformados no parsing de `Settings` desabilitam tracing sem relaxar a validação
das configurações funcionais. Falhas posteriores podem descartar o span.
Não é registrado shutdown automático no encerramento do processo; spans ainda
na fila podem ser perdidos se o chamador não fizer o flush explícito.

O caminho OTLP usa `BatchSpanProcessor`, sem I/O de exportação ou flush por
mensagem. O hook `configure_test_tracing` usa exportação síncrona exclusivamente
para testes offline. O parâmetro `export_timeout_millis` do
[BatchSpanProcessor 1.27](https://github.com/open-telemetry/opentelemetry-python/blob/v1.27.0/opentelemetry-sdk/src/opentelemetry/sdk/trace/export/__init__.py)
controla seu default de espera de flush; por isso recebe o orçamento de flush,
enquanto o exporter HTTP recebe o timeout de exportação separadamente.

`shutdown_tracing()` continua best-effort: absorve exceções, mas o `shutdown()` do
SDK espera o término da thread de exportação. O orçamento de flush não é um
limite total de shutdown ou dos retries HTTP do SDK. Não executar shutdown por
mensagem; este gate não certifica latência de encerramento em runtime.

## Allowlist de spans e atributos

A política central fica em `attention_router/observability/tracing.py` e aplica-se
a atributos iniciais, `safe_set_attribute`, chamadas diretas no Span retornado e
`current_span()`. Chaves desconhecidas, valores inválidos, listas e objetos são
descartados sem conversão para texto. Não há autorização baseada em substring,
sufixo ou prefixo de chave.

- Outcomes, modo de autonomia, resolução, origem da resposta e motivo de repetição usam enums fechados.
- Presença, supressão, resolução e permissões aceitam apenas booleanos.
- Contagens/comprimentos aceitam inteiros de 0 a 1.000.000; confiança aceita número finito entre 0 e 1; status HTTP aceita inteiro de 100 a 599.
- Correlações e referências internas explicitamente listadas aceitam apenas UUID canônico. IDs externos, telefone, identidade de ator e hashes de conteúdo/identidade são descartados.
- `roc.synthetic` aceita apenas `false`; `roc.trace_source` apenas `native`. A inclusão na allowlist não cria automaticamente esses atributos nem novos estágios.
- Até 32 atributos por span, strings até 128 caracteres (UUID: 36), zero eventos e até oito Links sem atributos ou tracestate.
- Nomes de span usam a lista das operações já existentes. Nomes desconhecidos são substituídos por `attention.operation`.

Objetivo, raciocínio, falha livre, corpo, telefone, nome pessoal, e-mail, arquivo,
prompt, resposta de IA, token, payload, URL, SQL e texto/stack de exceção não são
atributos permitidos. Hash de conteúdo também não é permitido neste gate.

Resources são construídos diretamente, sem `Resource.create()` ou detectores
que acrescentem atributos do ambiente. Somente as três chaves acima são aceitas:
`service.name` usa a lista fechada de processos/testes em `_RESOURCE_SERVICES`;
ambiente aceita `test`, `development`, `production` e `private`. Valores
personalizados exigem revisão explícita dessa lista, mesmo sob uma chave válida.

## Propagação

A metadata de fila existente continua carregando W3C `traceparent`. O helper
aceita o formato v00 canônico, IDs não nulos e flag `00`/`01`. Extrai sempre sobre
contexto vazio, confere o resultado e remove `tracestate`/`baggage`. Carrier
inexistente, inválido ou falha de extração gera contexto vazio; um consumidor
abre novo trace e nunca herda o item anterior. Contexto inválido passado
diretamente a `start_span` também é isolado.

Carrier válido preserva trace ID e parent span ID mesmo sob outro trace corrente.
O contexto anterior é restaurado ao sair do span, inclusive por erro funcional.
Sem argumento `context`, spans síncronos continuam filhos da operação corrente.
Links preservam apenas SpanContext válido, sem atributos ou tracestate.

Esse helper valida formato, não identidade/autoridade: o consumidor continua
responsável pelo vínculo autorizado da metadata ao trabalho/tenant.

### Internal Ingress — Etapa 1A

O Internal Ingress aceita `traceparent` W3C opcional exclusivamente como contexto
de observabilidade. O header não participa do body nem do HMAC e não concede
autoridade. Ele só é consumido depois de assinatura, payload, tenant explícito e
tenant ativo terem sido validados; requisição não autenticada não cria spans
ligados ao contexto recebido.

A rota transfere o `traceparent` explicitamente para o `ThreadPoolExecutor`, sem
depender de contexto ambiente da thread. `ingress.accept` usa o SpanContext remoto
válido como parent. Em seguida `attention.message` é aberto sobre contexto vazio,
portanto recebe um novo `trace_id`, e carrega um Span Link para a tentativa remota.
Depois que a recepção funcional cria ou recupera a identidade da Andy, o root
recebe `roc.correlation_id` com o mesmo valor retornado pelo Ingress e `roc.result`
`ACCEPTED` ou `DUPLICATE`. Replay preserva a correlação da Andy mesmo quando cada
tentativa HTTP pertence a outro trace.

`service.name` desse processo é inicializado como `attention-router-ingress`.
Carrier ausente ou inválido continua fail-open e abre traces locais limpos.

### Transport → Ingress — Etapa 1B

O Transport usa instrumentação manual, sem auto-instrumentação de `fetch`,
Puppeteer ou `whatsapp-web.js` e sem contexto global/AsyncLocalStorage. O SDK é
mantido explícito com `@opentelemetry/api` 1.9.1, core/resources/sdk-trace 2.11.0
e exporter OTLP HTTP/protobuf 0.222.0. O Resource do processo usa
`service.name=attention-router-transport`; `service.version` e ambiente continuam
validados e nenhum detector de ambiente acrescenta atributos arbitrários.

`transport.receive` envolve a operação real do bridge uma única vez. Na entrega
imediata, `transport.ingress_attempt` é filho explícito desse contexto. O body já
persistido no spool continua sendo o mesmo Buffer usado no HMAC e no `fetch`;
`traceparent` é injetado somente no objeto de headers, depois da assinatura, sem
`tracestate` ou `baggage`. O header não altera payload, idempotência ou autoridade.

O drainer de spool que não possui contexto transitório abre
`transport.ingress_attempt` como trace local independente. Persistência de
SpanContext ao lado do spool para continuidade após restart pertence à etapa
durável posterior; esta etapa não modifica o arquivo JSON funcional.

Tracing desabilitado, endpoint ausente ou configuração OTel inválida degrada para
no-op. Exceções do exporter são sanitizadas fora do caminho funcional; nenhuma
mensagem de erro é gravada em spans. Exportação usa `BatchSpanProcessor` e não há
flush por mensagem. O shutdown faz flush best-effort com espera bounded pelo
orçamento configurado antes da saída do processo.

## Exceções e status

`safe_record_exception` grava somente `error.type` de uma lista de classes builtin;
classes desconhecidas tornam-se `Exception`. A operação é idempotente por
atributo: nenhuma mensagem, stack, causa ou evento de exceção é exportado. A
captura automática do SDK fica desabilitada. Cada span que falha recebe outcome
`FAILED` e `ERROR`, sem descrição textual; isso não duplica eventos da exceção.

`set_outcome` usa `OK` para resultados funcionais como `SUPPRESSED`, `BLOCKED` e
`NOT_APPLICABLE`. Setup e cleanup de telemetria ficam fora do bloco funcional e
não substituem nem suprimem sua exceção. Exporters que lançam exceções são
encapsulados para evitar também o log automático da cadeia pelo processor.

A auditoria canônica `AUDITORIA_OTEL_NATIVO_20260924.md` permanece como registro
histórico. Gate 1 e Etapa 1A foram incorporados à `main`; a Etapa 1B completa o
carrier nativo da tentativa Transport → Ingress no código, mas ainda não certifica
Collector/Tempo, continuidade durável após restart nem rollout de runtime.
