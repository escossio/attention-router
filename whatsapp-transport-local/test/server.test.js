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

function postJson(port, path, body, secret) {
  const raw = Buffer.from(JSON.stringify(body));
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = `sha256=${crypto.createHmac('sha256', secret).update(`${timestamp}.${raw}`).digest('hex')}`;
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1', port, path, method: 'POST',
      headers: {
        'content-type': 'application/json', 'content-length': raw.length,
        'x-attention-timestamp': timestamp, 'x-attention-signature': signature,
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
