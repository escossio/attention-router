const test = require('node:test');
const assert = require('node:assert/strict');

const { InMemorySpanExporter } = require('@opentelemetry/sdk-trace');
const {
  createTransportTracing,
} = require('../src/observability');

const PRIVATE = 'synthetic-private phone:+5500000000000 token:not-real';

function testTracing(overrides = {}, exporter = new InMemorySpanExporter()) {
  const tracing = createTransportTracing({
    appEnv: 'test',
    otelServiceVersion: '0.1.0',
    otelTracesSampler: 'always_on',
    otelTracesSamplerArg: '1.0',
    ...overrides,
  }, { exporter });
  return { exporter, tracing };
}

test('manual transport spans propagate only W3C traceparent', async () => {
  const { exporter, tracing } = testTracing();
  let injected = null;
  await tracing.withSpan('transport.receive', null, async (receive) => {
    receive.setResult('DELIVERED');
    await tracing.withSpan('transport.ingress_attempt', receive.context, async (attempt) => {
      const headers = { baggage: PRIVATE, tracestate: PRIVATE };
      tracing.injectTraceparent(headers, attempt.context);
      injected = headers;
      attempt.setHttpStatus(202);
      attempt.setResult('ACCEPTED');
    });
  });

  assert.deepEqual(Object.keys(injected), ['traceparent']);
  assert.match(injected.traceparent, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 2);
  const receive = spans.find((span) => span.name === 'transport.receive');
  const attempt = spans.find((span) => span.name === 'transport.ingress_attempt');
  assert.equal(attempt.spanContext().traceId, receive.spanContext().traceId);
  assert.equal(attempt.parentSpanContext.spanId, receive.spanContext().spanId);
  assert.equal(receive.parentSpanContext, undefined);
  assert.equal(receive.attributes['roc.result'], 'DELIVERED');
  assert.equal(attempt.attributes['roc.result'], 'ACCEPTED');
  assert.equal(attempt.attributes['http.response.status_code'], 202);
  assert.equal(attempt.attributes['roc.trace_source'], 'native');
  assert.equal(attempt.attributes['roc.synthetic'], false);
  await tracing.shutdown();
});

test('outbound send extracts remote W3C parent and continues the same trace', async () => {
  const { exporter, tracing } = testTracing();
  const headers = {};
  await tracing.withSpan('transport.receive', null, async (receive) => {
    tracing.injectTraceparent(headers, receive.context);
    receive.setResult('DELIVERED');
  });

  const parent = tracing.extractTraceparent({
    traceparent: headers.traceparent,
    tracestate: PRIVATE,
    baggage: PRIVATE,
  });
  assert.ok(parent);

  await tracing.withSpan('transport.outbound_send', parent, async (send) => {
    send.setResult('DELIVERED');
    send.setHttpStatus(200);
  });

  const spans = exporter.getFinishedSpans();
  const receive = spans.find((span) => span.name === 'transport.receive');
  const outbound = spans.find((span) => span.name === 'transport.outbound_send');
  assert.equal(outbound.spanContext().traceId, receive.spanContext().traceId);
  assert.equal(outbound.parentSpanContext.spanId, receive.spanContext().spanId);
  assert.equal(outbound.parentSpanContext.isRemote, true);
  assert.equal(outbound.attributes['roc.result'], 'DELIVERED');
  assert.equal(outbound.attributes['http.response.status_code'], 200);
  assert.equal(tracing.extractTraceparent({ traceparent: 'invalid' }), null);
  await tracing.shutdown();
});

test('functional exception identity is preserved without exporting its message', async () => {
  const { exporter, tracing } = testTracing();
  const original = new TypeError(PRIVATE);
  let effects = 0;

  await assert.rejects(
    tracing.withSpan('transport.receive', null, async () => {
      effects += 1;
      throw original;
    }),
    (error) => error === original,
  );

  assert.equal(effects, 1);
  const [span] = exporter.getFinishedSpans();
  assert.equal(span.attributes['error.type'], 'TypeError');
  assert.equal(span.attributes['roc.result'], 'FAILED');
  assert.equal(span.events.length, 0);
  assert.equal(span.status.message, undefined);
  assert.equal(JSON.stringify(span.attributes).includes(PRIVATE), false);
  await tracing.shutdown();
});

test('broken exporter cannot change the functional result', async () => {
  const brokenExporter = {
    export() {
      throw new Error(PRIVATE);
    },
    async forceFlush() {},
    async shutdown() {},
  };
  const { tracing } = testTracing({}, brokenExporter);
  let effects = 0;

  const result = await tracing.withSpan('transport.receive', null, async (span) => {
    effects += 1;
    span.setResult('DELIVERED');
    return 42;
  });

  assert.equal(result, 42);
  assert.equal(effects, 1);
  await tracing.shutdown();
});

test('forceFlush stays bounded when exporter flush never settles', async () => {
  const exporter = {
    export(_spans, callback) {
      callback({ code: 0 });
    },
    forceFlush() {
      return new Promise(() => {});
    },
    async shutdown() {},
  };
  const { tracing } = testTracing({ otelFlushTimeoutMillis: '5' }, exporter);

  const started = Date.now();
  const flushed = await tracing.forceFlush();
  const elapsed = Date.now() - started;

  assert.equal(flushed, false);
  assert.equal(elapsed < 250, true);
  await tracing.shutdown();
});

test('invalid production telemetry configuration falls back to no-op', async () => {
  const tracing = createTransportTracing({
    otelTracingEnabled: true,
    otelExporterOtlpEndpoint: 'ftp://collector.invalid/v1/traces',
    otelExporterOtlpProtocol: 'http/protobuf',
    otelServiceVersion: '0.1.0',
  });
  assert.equal(tracing.enabled, false);

  const original = new Error(PRIVATE);
  let effects = 0;
  await assert.rejects(
    tracing.withSpan('transport.receive', null, async () => {
      effects += 1;
      throw original;
    }),
    (error) => error === original,
  );
  assert.equal(effects, 1);
});

test('disabled tracing is a true no-op', async () => {
  const tracing = createTransportTracing({ otelTracingEnabled: false });
  let effects = 0;
  const result = await tracing.withSpan('transport.receive', null, async (span) => {
    effects += 1;
    assert.equal(span.context, null);
    return 'ok';
  });
  assert.equal(result, 'ok');
  assert.equal(effects, 1);
  const headers = {};
  tracing.injectTraceparent(headers, null);
  assert.deepEqual(headers, {});
  assert.equal(tracing.extractTraceparent({ traceparent: 'invalid' }), null);
});

test('transport resource is explicit and ignores arbitrary OTel environment attributes', async () => {
  const previous = process.env.OTEL_RESOURCE_ATTRIBUTES;
  process.env.OTEL_RESOURCE_ATTRIBUTES = `host.name=${PRIVATE},service.instance.id=${PRIVATE}`;
  try {
    const { exporter, tracing } = testTracing();
    await tracing.withSpan('transport.receive', null, async (span) => {
      span.setResult('DELIVERED');
    });
    const [span] = exporter.getFinishedSpans();
    assert.deepEqual(span.resource.attributes, {
      'service.name': 'attention-router-transport',
      'service.version': '0.1.0',
      'deployment.environment': 'test',
    });
    assert.equal(JSON.stringify(span.resource.attributes).includes(PRIVATE), false);
    await tracing.shutdown();
  } finally {
    if (previous === undefined) delete process.env.OTEL_RESOURCE_ATTRIBUTES;
    else process.env.OTEL_RESOURCE_ATTRIBUTES = previous;
  }
});
