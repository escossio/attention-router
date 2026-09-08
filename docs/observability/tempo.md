# Live Flow — Etapa 2: Collector e Tempo

## Arquitetura

O candidate sintético usa a rota:

```text
Attention Router / trace-demo --OTLP HTTP--> otel-collector:4318
otel-collector --OTLP gRPC--> tempo:4317
Tempo HTTP query API --> 127.0.0.1:3200
```

`compose.observability.yaml` é uma stack separada. O tráfego entre os serviços usa
a rede Docker interna `attention_router_observability`; uma segunda rede técnica
existe somente para os bindings de host em loopback. A stack publica somente
health/OTLP/query em loopback e
não reinicia API, worker, PostgreSQL, ingress, transport, Chrome ou Xvfb.

Imagens são fixadas em `otel/opentelemetry-collector-contrib:0.153.0` e
`grafana/tempo:3.0.2`; `latest` não é usado.

## Inicialização e portas

```bash
docker compose -p attention-router-live-flow -f compose.observability.yaml up -d tempo otel-collector
until curl -fsS http://127.0.0.1:3200/ready >/dev/null; do sleep 1; done
until curl -fsS http://127.0.0.1:13133/ >/dev/null; do sleep 1; done
```

Portas internas e bindings:

| Serviço | Porta | Exposição |
|---|---:|---|
| Collector OTLP gRPC | 4317 | `127.0.0.1` |
| Collector OTLP HTTP | 4318 | `127.0.0.1` |
| Collector health | 13133 | `127.0.0.1` |
| Collector internal metrics | 8888 | `127.0.0.1` |
| Tempo query/ready | 3200 | `127.0.0.1` |

Não há Grafana, Collector/Tempo não são publicados em LAN/Internet e o runtime
vivo permanece com `OTEL_TRACING_ENABLED=false`.

As imagens distroless não possuem shell/curl para um healthcheck HTTP interno.
O Compose usa um healthcheck de executabilidade da imagem; os endpoints `/ready`
e `/` são sempre verificados externamente antes dos testes E2E.

## Retenção e armazenamento

Tempo usa armazenamento local monolítico, WAL e blocos em
`/var/tempo`, persistidos no volume Docker `attention_router_tempo_data`.
`block_retention: 72h` limita a retenção operacional. O volume não é o banco do
Attention Router e não mistura dados com PostgreSQL.

Collector usa `memory_limiter`, `batch`, fila de export bounded (`queue_size: 256`)
e retry limitado a 30 segundos. O aplicativo usa `BatchSpanProcessor` com fila e
lotes configuráveis; falha de backend não bloqueia o domínio.

O endpoint Prometheus em `127.0.0.1:8888/metrics` é somente a telemetria interna
do Collector, não um servidor Prometheus. Ele permite auditar
`otelcol_receiver_accepted_spans` e `otelcol_exporter_sent_spans` sem depender de
logs.

## Candidate e consulta

```bash
docker compose -p attention-router-live-flow -f compose.observability.yaml \
  --profile candidate run --rm trace-demo

.venv/bin/python scripts/tempo_trace.py <trace-id>
```

O helper é somente leitura e imprime nomes, duração, status e uma lista allowlist
de outcomes/repetition/response source. Não imprime o payload bruto do Tempo.

Para gerar a trilha de supressão, use `TRACE_DEMO_PATH=suppressed`; para verificar
o filtro persistido, use `TRACE_DEMO_PATH=privacy`. Esses cenários são sintéticos:
não representam runtime nem mensagem WhatsApp real.

## Falhas e recuperação

Collector, Tempo ou ambos podem ser parados sem alterar a execução funcional do
candidate. Exporter, flush e shutdown são best-effort; o BatchSpanProcessor tem
limites explícitos. Depois que o backend volta, um trace novo deve ser gerado para
provar recuperação. Um trace armazenado no volume deve continuar consultável após
restart isolado do Tempo.

## Caminho futuro do Codex

O caminho read-only é `scripts/tempo_trace.py <trace_id>` sobre
`http://127.0.0.1:3200`. Esta etapa não implementa watcher, ações automáticas,
ações automáticas ou habilitação no runtime. Grafana, quando iniciado na Etapa 3,
usa a mesma API e não é dependência deste caminho.
