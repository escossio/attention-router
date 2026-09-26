const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-(?:00|01)$/;
const SPAN_NAMES = new Set(['transport.receive', 'transport.ingress_attempt', 'transport.outbound_send']);
const RESULTS = new Set([
  'ACCEPTED', 'AUTH_FAILED', 'BAD_PAYLOAD', 'BLOCKED', 'CONFLICT',
  'DELIVERED', 'DUPLICATE', 'FAILED', 'IGNORED', 'MISSING', 'RETRY',
]);
const ENVIRONMENTS = new Set(['test', 'development', 'production', 'private']);
const VERSION = /^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$/;
const SERVICE_NAME = 'attention-router-transport';

function noopScope() {
  return {
    context: null,
    setResult() {},
    setHttpStatus() {},
    recordError() {},
    end() {},
  };
}

function createNoopTransportTracing() {
  return {
    enabled: false,
    async withSpan(_name, _parentContext, operation) {
      return operation(noopScope());
    },
    injectTraceparent(headers) {
      if (headers && typeof headers === 'object') {
        delete headers.traceparent;
        delete headers.tracestate;
        delete headers.baggage;
      }
      return headers;
    },
    extractTraceparent() {
      return null;
    },
    async forceFlush() {
      return false;
    },
    async shutdown() {},
  };
}

function boundedInteger(value, fallback, minimum, maximum) {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error('invalid telemetry integer');
  }
  return parsed;
}

function boundedRatio(value, fallback = 1) {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    throw new Error('invalid telemetry ratio');
  }
  return parsed;
}

function normalizeEndpoint(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const parsed = new URL(value);
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('invalid telemetry endpoint');
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('invalid telemetry endpoint');
  }
  return parsed.toString();
}

function normalizeVersion(value) {
  const candidate = typeof value === 'string' ? value : '0.1.0';
  if (!VERSION.test(candidate)) throw new Error('invalid telemetry service version');
  return candidate;
}

function samplerFor(config, sdk) {
  const value = config.otelTracesSampler || 'parentbased_traceidratio';
  if (value === 'always_on') return new sdk.AlwaysOnSampler();
  if (value === 'always_off') return new sdk.AlwaysOffSampler();
  if (value !== 'parentbased_traceidratio') throw new Error('invalid telemetry sampler');
  return new sdk.ParentBasedSampler({
    root: new sdk.TraceIdRatioBasedSampler(boundedRatio(config.otelTracesSamplerArg, 1)),
  });
}

function sanitizedErrorType(error) {
  if (error?.constructor === TypeError) return 'TypeError';
  if (error?.constructor === RangeError) return 'RangeError';
  if (error?.constructor === ReferenceError) return 'ReferenceError';
  if (typeof DOMException !== 'undefined' && error?.constructor === DOMException
      && error.name === 'AbortError') return 'AbortError';
  return 'Error';
}

function sanitizedExporter(exporter, ExportResultCode) {
  return {
    export(spans, callback) {
      let completed = false;
      const finish = (success) => {
        if (completed) return;
        completed = true;
        try {
          callback({
            code: success ? ExportResultCode.SUCCESS : ExportResultCode.FAILED,
          });
        } catch {}
      };
      try {
        exporter.export(spans, (result) => {
          finish(result?.code === ExportResultCode.SUCCESS);
        });
      } catch {
        finish(false);
      }
    },
    async forceFlush() {
      try {
        await exporter.forceFlush?.();
      } catch {}
    },
    async shutdown() {
      try {
        await exporter.shutdown?.();
      } catch {}
    },
  };
}

function settleWithin(promise, timeoutMillis) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => finish(false), timeoutMillis);
    timer.unref?.();
    Promise.resolve(promise).then(
      () => {
        clearTimeout(timer);
        finish(true);
      },
      () => {
        clearTimeout(timer);
        finish(false);
      },
    );
  });
}

function createTransportTracing(config = {}, deps = {}) {
  if (config.otelTracingEnabled !== true && !deps.exporter) {
    return createNoopTransportTracing();
  }
  try {
    const api = require('@opentelemetry/api');
    const { ExportResultCode, W3CTraceContextPropagator } = require('@opentelemetry/core');
    const { resourceFromAttributes } = require('@opentelemetry/resources');
    const sdk = require('@opentelemetry/sdk-trace');
    const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');

    const version = normalizeVersion(config.otelServiceVersion);
    const resourceAttributes = {
      'service.name': SERVICE_NAME,
      'service.version': version,
    };
    if (ENVIRONMENTS.has(config.appEnv)) {
      resourceAttributes['deployment.environment'] = config.appEnv;
    }

    let exporter = deps.exporter || null;
    let processor = null;
    if (exporter) {
      processor = new sdk.SimpleSpanProcessor({
        exporter: sanitizedExporter(exporter, ExportResultCode),
      });
    } else {
      if ((config.otelExporterOtlpProtocol || 'http/protobuf') !== 'http/protobuf') {
        throw new Error('invalid telemetry protocol');
      }
      const endpoint = normalizeEndpoint(config.otelExporterOtlpEndpoint);
      if (!endpoint) return createNoopTransportTracing();
      const exportTimeout = boundedInteger(
        config.otelExportTimeoutMillis,
        5000,
        1,
        10000,
      );
      exporter = new OTLPTraceExporter({
        url: endpoint,
        timeoutMillis: exportTimeout,
        concurrencyLimit: 2,
      });
      const queue = boundedInteger(config.otelBatchMaxQueueSize, 2048, 1, 8192);
      const batch = boundedInteger(
        config.otelBatchMaxExportBatchSize,
        512,
        1,
        Math.min(queue, 1024),
      );
      processor = new sdk.BatchSpanProcessor({
        exporter: sanitizedExporter(exporter, ExportResultCode),
        maxQueueSize: queue,
        maxExportBatchSize: batch,
        scheduledDelayMillis: boundedInteger(
          config.otelBatchScheduleDelayMillis,
          500,
          10,
          60000,
        ),
        exportTimeoutMillis: exportTimeout,
      });
    }

    const provider = new sdk.TracerProvider({
      resource: resourceFromAttributes(resourceAttributes),
      sampler: samplerFor(config, sdk),
      spanLimits: {
        attributeValueLengthLimit: 128,
        attributeCountLimit: 8,
        linkCountLimit: 0,
        eventCountLimit: 0,
        attributePerEventCountLimit: 0,
        attributePerLinkCountLimit: 0,
      },
      spanProcessors: [processor],
    });
    const tracer = provider.getTracer('attention-router-transport', version);
    const propagator = new W3CTraceContextPropagator();
    const flushTimeout = boundedInteger(config.otelFlushTimeoutMillis, 1000, 0, 10000);

    const safeSet = (span, key, value) => {
      try {
        if (key === 'roc.trace_source' && value === 'native') span.setAttribute(key, value);
        else if (key === 'roc.synthetic' && value === false) span.setAttribute(key, value);
        else if (key === 'roc.result' && RESULTS.has(value)) span.setAttribute(key, value);
        else if (key === 'http.response.status_code' && Number.isInteger(value)
          && value >= 100 && value <= 599) span.setAttribute(key, value);
        else if (key === 'error.type' && ['Error', 'TypeError', 'RangeError', 'ReferenceError', 'AbortError'].includes(value)) {
          span.setAttribute(key, value);
        }
      } catch {}
    };

    const startScope = (name, parentContext = null) => {
      if (!SPAN_NAMES.has(name)) return noopScope();
      let span;
      try {
        span = tracer.startSpan(name, {}, parentContext || api.ROOT_CONTEXT);
      } catch {
        return noopScope();
      }
      let ended = false;
      let failed = false;
      safeSet(span, 'roc.trace_source', 'native');
      safeSet(span, 'roc.synthetic', false);
      let childContext = null;
      try {
        childContext = api.trace.setSpan(api.ROOT_CONTEXT, span);
      } catch {}

      return {
        context: childContext,
        setResult(value) {
          safeSet(span, 'roc.result', value);
        },
        setHttpStatus(value) {
          safeSet(span, 'http.response.status_code', value);
        },
        recordError(error) {
          failed = true;
          safeSet(span, 'roc.result', 'FAILED');
          safeSet(span, 'error.type', sanitizedErrorType(error));
          try {
            span.setStatus({ code: api.SpanStatusCode.ERROR });
          } catch {}
        },
        end() {
          if (ended) return;
          ended = true;
          if (!failed) {
            try {
              span.setStatus({ code: api.SpanStatusCode.OK });
            } catch {}
          }
          try {
            span.end();
          } catch {}
        },
      };
    };

    const withSpan = async (name, parentContext, operation) => {
      let scope = noopScope();
      try {
        scope = startScope(name, parentContext);
      } catch {}
      try {
        return await operation(scope);
      } catch (error) {
        scope.recordError(error);
        throw error;
      } finally {
        scope.end();
      }
    };

    const injectTraceparent = (headers, context) => {
      if (!headers || typeof headers !== 'object') return headers;
      delete headers.traceparent;
      delete headers.tracestate;
      delete headers.baggage;
      if (!context) return headers;
      try {
        const carrier = {};
        propagator.inject(context, carrier, {
          set(target, key, value) {
            if (key === 'traceparent' && typeof value === 'string') target.traceparent = value;
          },
        });
        if (TRACEPARENT.test(carrier.traceparent || '')) {
          headers.traceparent = carrier.traceparent;
        }
      } catch {}
      return headers;
    };

    const extractTraceparent = (headers) => {
      try {
        if (!headers || typeof headers !== 'object') return null;
        const raw = headers.traceparent;
        const parent = Array.isArray(raw) ? raw[0] : raw;
        if (typeof parent !== 'string' || !TRACEPARENT.test(parent)) return null;
        const carrier = { traceparent: parent };
        const extracted = propagator.extract(api.ROOT_CONTEXT, carrier, {
          get(target, key) {
            return key === 'traceparent' ? target.traceparent : undefined;
          },
          keys() {
            return ['traceparent'];
          },
        });
        const spanContext = api.trace.getSpanContext(extracted);
        if (!spanContext || !api.isSpanContextValid(spanContext)
            || spanContext.traceId !== parent.slice(3, 35)
            || spanContext.spanId !== parent.slice(36, 52)) {
          return null;
        }
        return extracted;
      } catch {
        return null;
      }
    };

    const forceFlush = async (timeoutMillis = flushTimeout) => {
      try {
        const timeout = boundedInteger(timeoutMillis, flushTimeout, 0, 10000);
        return await settleWithin(provider.forceFlush({ timeoutMillis: timeout }), timeout + 25);
      } catch {
        return false;
      }
    };

    const shutdown = async () => {
      await forceFlush();
      try {
        await settleWithin(provider.shutdown(), flushTimeout + 25);
      } catch {}
    };

    return {
      enabled: true,
      withSpan,
      injectTraceparent,
      extractTraceparent,
      forceFlush,
      shutdown,
    };
  } catch {
    return createNoopTransportTracing();
  }
}

module.exports = {
  createNoopTransportTracing,
  createTransportTracing,
};
