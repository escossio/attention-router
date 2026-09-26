const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const pathModule = require('node:path');

const { createConfig } = require('../src/config');
const { createStatus } = require('../src/status');
const { createServer } = require('../src/server');
const { createTransportTracing } = require('../src/observability');
const { InMemorySpanExporter } = require('@opentelemetry/sdk-trace');

function request(port, path) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method: 'GET' }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        resolve({
          statusCode: res.statusCode,
          body: Buffer.concat(chunks).toString('utf8'),
        });
      });
    });
    req.on('error', reject);
    req.end();
  });
}

function postJson(port, path, body, secret, extraHeaders = {}) {
  const raw = Buffer.from(JSON.stringify(body));
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.${raw}`).digest('hex')}`;
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, path, method: 'POST',
      headers: {
        'content-type': 'application/json', 'content-length': raw.length,
        'x-attention-timestamp': timestamp, 'x-attention-signature': signature,
        ...extraHeaders,
      },
    }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({ statusCode: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) }));
    });
    req.on('error', reject);
    req.end(raw);
  });
}

test('status server exposes live ready and status read-only', async () => {
  const config = createConfig({ LOCAL_TRANSPORT_HTTP_PORT: '0' });
  const status = createStatus(config, {
    service_state: 'ready',
    browser_debug_reachable: true,
    wwebjs_connected: true,
    ready: true,
    client_state: 'CONNECTED',
  });
  const server = createServer(config, status);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  const live = await request(address.port, '/live');
  assert.equal(live.statusCode, 200);
  assert.match(live.body, /"status":"live"/);

  const ready = await request(address.port, '/ready');
  assert.equal(ready.statusCode, 200);
  assert.match(ready.body, /"status":"ready"/);

  const snapshot = await request(address.port, '/status');
  assert.equal(snapshot.statusCode, 200);
  assert.match(snapshot.body, /"service_state":"ready"/);

  await new Promise((resolve) => server.close(resolve));
});

test('ready endpoint fails closed for unknown and disconnected states', async () => {
  const config = createConfig({ LOCAL_TRANSPORT_HTTP_PORT: '0' });
  const server = createServer(config, createStatus(config));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  const notReady = await request(address.port, '/ready');
  assert.equal(notReady.statusCode, 503);
  assert.match(notReady.body, /"status":"not_ready"/);

  await new Promise((resolve) => server.close(resolve));
});

test('status server keeps sensitive fields out of the read-only payload', async () => {
  const config = createConfig({ LOCAL_TRANSPORT_HTTP_PORT: '0' });
  const status = createStatus(config, {
    service_state: 'ready',
    browser_debug_reachable: true,
    wwebjs_connected: true,
    ready: true,
    client_state: 'CONNECTED',
    qr_seen: false,
  });
  const server = createServer(config, status);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');

  const snapshot = await request(address.port, '/status');
  assert.equal(snapshot.statusCode, 200);
  assert.doesNotMatch(snapshot.body, /sendMessage|cookie|token|phone|chat|contact/i);

  await new Promise((resolve) => server.close(resolve));
});

test('authenticated outbound endpoint sends once and deduplicates delivery', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';
  const provenanceDir = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-provenance-'));
  t.after(() => fs.rmSync(provenanceDir, { recursive: true, force: true }));
  const config = createConfig({ LOCAL_TRANSPORT_HTTP_PORT: '0', EXTERNAL_DELIVERY_ENABLED: 'true', LOCAL_OUTBOUND_HMAC_SECRET: secret, LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir });
  const status = createStatus(config, { service_state: 'ready', browser_debug_reachable: true, wwebjs_connected: true, ready: true, client_state: 'CONNECTED', qr_seen: false });
  let sends = 0;
  const server = createServer(config, status, { sendMessage: async () => { sends += 1; return { id: { _serialized: 'message-ref' } }; } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const payload = { idempotency_key: 'execution:test', destination: 'contact@c.us', text: 'controlled response' };
  const first = await postJson(address.port, '/internal/send', payload, secret);
  const second = await postJson(address.port, '/internal/send', payload, secret);
  assert.equal(first.statusCode, 200);
  assert.equal(first.body.status, 'sent');
  assert.equal(second.body.status, 'already_sent');
  assert.equal(sends, 1);
  await new Promise((resolve) => server.close(resolve));
});

test('outbound send continues an incoming traceparent into the real sendMessage span', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';
  const provenanceDir = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-provenance-'));
  t.after(() => fs.rmSync(provenanceDir, { recursive: true, force: true }));
  const config = createConfig({
    LOCAL_TRANSPORT_HTTP_PORT: '0',
    EXTERNAL_DELIVERY_ENABLED: 'true',
    LOCAL_OUTBOUND_HMAC_SECRET: secret,
    LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir,
  });
  const status = createStatus(config, {
    service_state: 'ready',
    browser_debug_reachable: true,
    wwebjs_connected: true,
    ready: true,
    client_state: 'CONNECTED',
    qr_seen: false,
  });
  const calls = [];
  const parentContext = { marker: 'remote-parent' };
  const observability = {
    extractTraceparent(headers) {
      calls.push(['extract', headers.traceparent]);
      return parentContext;
    },
    async withSpan(name, parent, operation) {
      calls.push(['span', name, parent]);
      const scope = {
        setResult(value) { calls.push(['result', value]); },
        setHttpStatus(value) { calls.push(['http', value]); },
      };
      return operation(scope);
    },
  };
  const client = {
    sendMessage: async () => ({ id: { _serialized: 'otel-message-ref' } }),
  };
  const server = createServer(config, status, client, { observability });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const traceparent = '00-1234567890abcdef1234567890abcdef-1234567890abcdef-01';
  const response = await postJson(
    address.port,
    '/internal/send',
    {
      idempotency_key: 'execution:otel-outbound',
      destination: 'contact@c.us',
      text: 'controlled response',
    },
    secret,
    { traceparent },
  );
  assert.equal(response.statusCode, 200);
  assert.deepEqual(calls[0], ['extract', traceparent]);
  assert.deepEqual(calls[1], ['span', 'transport.outbound_send', parentContext]);
  assert.ok(calls.some((item) => item[0] === 'result' && item[1] === 'DELIVERED'));
  assert.ok(calls.some((item) => item[0] === 'http' && item[1] === 200));
  await new Promise((resolve) => server.close(resolve));
});

test('real outbound telemetry preserves remote parent, privacy and durable replay', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';
  const provenanceDir = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-otel-'));
  const exporter = new InMemorySpanExporter();
  const observability = createTransportTracing({ appEnv: 'test' }, { exporter });
  t.after(async () => {
    await observability.shutdown();
    fs.rmSync(provenanceDir, { recursive: true, force: true });
  });
  const config = createConfig({ LOCAL_TRANSPORT_HTTP_PORT: '0', EXTERNAL_DELIVERY_ENABLED: 'true',
    LOCAL_OUTBOUND_HMAC_SECRET: secret, LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir });
  const status = createStatus(config, { service_state: 'ready', browser_debug_reachable: true,
    wwebjs_connected: true, ready: true, client_state: 'CONNECTED', qr_seen: false });
  let sends = 0;
  const client = { async sendMessage() {
    sends += 1;
    // The enclosing send span must still be open at the real effect boundary.
    assert.equal(exporter.getFinishedSpans().length, sends - 1);
    return { id: { _serialized: 'private-provider-reference' } };
  } };
  const payload = { idempotency_key: 'execution:private-key', destination: 'private-peer@c.us', text: 'private-body' };
  const headers = { traceparent: '00-1234567890abcdef1234567890abcdef-1234567890abcdef-01',
    baggage: 'private-baggage', tracestate: 'vendor=private-state' };
  async function start() {
    const server = createServer(config, status, client, { observability, logger: { error() {} } });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    t.after(() => new Promise((resolve) => server.close(resolve)));
    return server.address().port;
  }
  const port = await start();
  assert.equal((await postJson(port, '/internal/send', payload, secret, headers)).body.status, 'sent');
  assert.equal((await postJson(port, '/internal/send', payload, secret, headers)).body.status, 'already_sent');
  const restartedPort = await start();
  assert.equal((await postJson(restartedPort, '/internal/send', payload, secret, headers)).body.status, 'already_sent');
  assert.equal(sends, 1);
  const [span] = exporter.getFinishedSpans();
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal(span.name, 'transport.outbound_send');
  assert.equal(span.spanContext().traceId, '1234567890abcdef1234567890abcdef');
  assert.equal(span.parentSpanContext.spanId, '1234567890abcdef');
  assert.equal(span.parentSpanContext.isRemote, true);
  assert.equal(span.resource.attributes['service.name'], 'attention-router-transport');
  assert.equal(span.attributes['roc.result'], 'DELIVERED');
  assert.equal(span.status.code, 1);
  assert.equal(span.events.length, 0);
  assert.doesNotMatch(JSON.stringify([span.attributes, span.resource.attributes, span.status, span.links]), /private-|execution:/);

  const invalid = await postJson(port, '/internal/send', { ...payload, idempotency_key: 'execution:invalid-parent' }, secret,
    { traceparent: 'invalid-private-parent' });
  assert.equal(invalid.body.status, 'sent');
  assert.equal(sends, 2);
  assert.equal(exporter.getFinishedSpans()[1].parentSpanContext, undefined);

  const voice = await postJson(port, '/internal/send', {
    idempotency_key: 'execution:missing-media', external_actor_id: payload.destination, message_type: 'ptt',
    media_ref: `sha256:${'a'.repeat(64)}`, content_sha256: 'a'.repeat(64), mime_type: 'audio/ogg',
    size_bytes: 10, execution_intent_id: 'intent-fixture', outbox_id: 'outbox-fixture',
  }, secret, headers);
  assert.equal(voice.statusCode, 503);
  assert.equal(sends, 2);
  assert.equal(exporter.getFinishedSpans().length, 2);
});

test('voice outbound uses MessageMedia and sendAudioAsVoice', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';
  const provenanceDir = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-provenance-'));
  const mediaRoot = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-media-'));
  t.after(() => fs.rmSync(provenanceDir, { recursive: true, force: true }));
  t.after(() => fs.rmSync(mediaRoot, { recursive: true, force: true }));
  const bytes = Buffer.from('ID3 voice');
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');
  fs.mkdirSync(pathModule.join(mediaRoot, digest.slice(0, 2)), { recursive: true });
  fs.writeFileSync(pathModule.join(mediaRoot, digest.slice(0, 2), digest), bytes);
  const config = createConfig({
    LOCAL_TRANSPORT_HTTP_PORT: '0', EXTERNAL_DELIVERY_ENABLED: 'true',
    LOCAL_OUTBOUND_HMAC_SECRET: secret, LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir,
    WHATSAPP_MEDIA_ROOT: mediaRoot, WHATSAPP_MEDIA_MAX_BYTES: '1024',
  });
  const status = createStatus(config, { service_state: 'ready', browser_debug_reachable: true,
    wwebjs_connected: true, ready: true, client_state: 'CONNECTED', qr_seen: false });
  class FakeMessageMedia { constructor(mimetype, data) { this.mimetype = mimetype; this.data = data; } }
  const calls = [];
  const client = { sendMessage: async (...args) => {
    calls.push(args); return { id: { _serialized: 'voice-message-ref' } };
  } };
  const server = createServer(config, status, client, { MessageMedia: FakeMessageMedia });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const response = await postJson(address.port, '/internal/send', {
    idempotency_key: 'execution:voice', external_actor_id: 'contact@c.us', message_type: 'ptt',
    media_ref: `sha256:${digest}`, content_sha256: digest, mime_type: 'audio/mpeg',
    size_bytes: bytes.length, execution_intent_id: 'intent-voice', outbox_id: 'outbox-voice',
  }, secret);
  assert.equal(response.statusCode, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].mimetype, 'audio/mpeg');
  assert.deepEqual(calls[0][2], { sendAudioAsVoice: true });
  await new Promise((resolve) => server.close(resolve));
});

test('Ogg/Opus voice outbound preserves audio/ogg MIME and PTT flag', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';
  const provenanceDir = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-provenance-'));
  const mediaRoot = fs.mkdtempSync(pathModule.join(os.tmpdir(), 'attention-server-media-'));
  t.after(() => fs.rmSync(provenanceDir, { recursive: true, force: true }));
  t.after(() => fs.rmSync(mediaRoot, { recursive: true, force: true }));
  const bytes = Buffer.from('OggS Opus voice');
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');
  fs.mkdirSync(pathModule.join(mediaRoot, digest.slice(0, 2)), { recursive: true });
  fs.writeFileSync(pathModule.join(mediaRoot, digest.slice(0, 2), digest), bytes);
  const config = createConfig({
    LOCAL_TRANSPORT_HTTP_PORT: '0', EXTERNAL_DELIVERY_ENABLED: 'true',
    LOCAL_OUTBOUND_HMAC_SECRET: secret, LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir,
    WHATSAPP_MEDIA_ROOT: mediaRoot, WHATSAPP_MEDIA_MAX_BYTES: '1024',
  });
  const status = createStatus(config, { service_state: 'ready', browser_debug_reachable: true,
    wwebjs_connected: true, ready: true, client_state: 'CONNECTED', qr_seen: false });
  class FakeMessageMedia { constructor(mimetype, data) { this.mimetype = mimetype; this.data = data; } }
  const calls = [];
  const client = { sendMessage: async (...args) => {
    calls.push(args); return { id: { _serialized: 'ogg-voice-message-ref' } };
  } };
  const server = createServer(config, status, client, { MessageMedia: FakeMessageMedia });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const response = await postJson(address.port, '/internal/send', {
    idempotency_key: 'execution:ogg-voice', external_actor_id: 'contact@c.us', message_type: 'ptt',
    media_ref: `sha256:${digest}`, content_sha256: digest, mime_type: 'audio/ogg',
    size_bytes: bytes.length, execution_intent_id: 'intent-ogg-voice', outbox_id: 'outbox-ogg-voice',
  }, secret);
  assert.equal(response.statusCode, 200);
  assert.equal(calls[0][1].mimetype, 'audio/ogg');
  assert.deepEqual(calls[0][2], { sendAudioAsVoice: true });
  await new Promise((resolve) => server.close(resolve));
});


test('outbound voice send failure logs sanitized diagnostics and stays fail-closed', async (t) => {
  const secret = 'unit-test-outbound-secret-32-bytes-minimum';

  const provenanceDir = fs.mkdtempSync(
    pathModule.join(os.tmpdir(), 'attention-server-provenance-')
  );

  const mediaRoot = fs.mkdtempSync(
    pathModule.join(os.tmpdir(), 'attention-server-media-')
  );

  t.after(() => fs.rmSync(provenanceDir, { recursive: true, force: true }));
  t.after(() => fs.rmSync(mediaRoot, { recursive: true, force: true }));

  const bytes = Buffer.from('ID3 voice diagnostic');
  const digest = crypto.createHash('sha256').update(bytes).digest('hex');

  fs.mkdirSync(
    pathModule.join(mediaRoot, digest.slice(0, 2)),
    { recursive: true }
  );

  fs.writeFileSync(
    pathModule.join(mediaRoot, digest.slice(0, 2), digest),
    bytes
  );

  const config = createConfig({
    LOCAL_TRANSPORT_HTTP_PORT: '0',
    EXTERNAL_DELIVERY_ENABLED: 'true',
    LOCAL_OUTBOUND_HMAC_SECRET: secret,
    LOCAL_OUTBOUND_PROVENANCE_DIR: provenanceDir,
    WHATSAPP_MEDIA_ROOT: mediaRoot,
    WHATSAPP_MEDIA_MAX_BYTES: '1024',
  });

  const status = createStatus(config, {
    service_state: 'ready',
    browser_debug_reachable: true,
    wwebjs_connected: true,
    ready: true,
    client_state: 'CONNECTED',
    qr_seen: false,
  });

  class FakeMessageMedia {
    constructor(mimetype, data) {
      this.mimetype = mimetype;
      this.data = data;
    }
  }

  const logs = [];

  const logger = {
    error: (line) => logs.push(String(line)),
  };

  const client = {
    sendMessage: async () => {
      throw new Error(
        'Evaluation failed: Error: t: t for 5500000000029@lid https://example.invalid/private'
      );
    },
  };

  const server = createServer(
    config,
    status,
    client,
    {
      MessageMedia: FakeMessageMedia,
      logger,
    }
  );

  await new Promise(
    (resolve) => server.listen(0, '127.0.0.1', resolve)
  );

  const address = server.address();

  const response = await postJson(
    address.port,
    '/internal/send',
    {
      idempotency_key: 'execution:voice-error-diagnostic',
      external_actor_id: 'contact@lid',
      message_type: 'ptt',
      media_ref: `sha256:${digest}`,
      content_sha256: digest,
      mime_type: 'audio/mpeg',
      size_bytes: bytes.length,
      execution_intent_id: 'intent-voice-error',
      outbox_id: 'outbox-voice-error',
    },
    secret
  );

  assert.equal(response.statusCode, 503);
  assert.equal(response.body.status, 'send_failed');

  assert.equal(logs.length, 1);

  const diagnostic = JSON.parse(logs[0]);

  assert.equal(diagnostic.event, 'outbound_send_failed');
  assert.equal(diagnostic.message_type, 'ptt');
  assert.equal(diagnostic.error_class, 'Error');

  assert.match(
    diagnostic.error_message,
    /Evaluation failed: Error: t: t/
  );

  assert.doesNotMatch(
    diagnostic.error_message,
    /5500000000029/
  );

  assert.doesNotMatch(
    diagnostic.error_message,
    /https:\/\/example\.invalid/
  );

  const files = fs.readdirSync(provenanceDir);

  assert.equal(files.length, 1);

  const provenance = JSON.parse(
    fs.readFileSync(
      pathModule.join(provenanceDir, files[0]),
      'utf8'
    )
  );

  assert.equal(provenance.status, 'FAILED');
  assert.equal(provenance.failure_reason, 'Error');

  await new Promise(
    (resolve) => server.close(resolve)
  );
});
