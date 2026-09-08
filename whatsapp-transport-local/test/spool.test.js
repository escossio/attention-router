const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createInboundBridge } = require('../src/bridge');
const { deliverPendingFile, enqueuePending, ensureSpool, listPending } = require('../src/spool');

function tempConfig(overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-router-stage37-spool-'));
  const config = {
    inboundSpoolDir: path.join(root, 'spool'),
    inboundPendingDir: path.join(root, 'spool', 'pending'),
    inboundSendingDir: path.join(root, 'spool', 'sending'),
    inboundSentDir: path.join(root, 'spool', 'sent'),
    inboundQuarantineDir: path.join(root, 'spool', 'quarantine'),
    inboundForwardEnabled: false,
    inboundForwardUrl: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
    inboundHttpTimeoutMs: 200,
    inboundMaxSkewSeconds: 300,
    inboundSource: 'wwebjs',
    inboundChannel: 'whatsapp',
    hmacSecret: 'unit-test-inbound-secret-32-bytes-minimum',
    ...overrides,
  };
  return { root, config };
}

test('inbound bridge fails closed when forward is disabled', async () => {
  const { root, config } = tempConfig();
  const counters = [];
  const bridge = createInboundBridge(config, { info() {}, error() {}, warn() {} }, {
    fetchImpl: async () => {
      throw new Error('fetch should not be called');
    },
    onCounters: (value) => counters.push(value),
  });

  const result = await bridge.handleMessage({
    id: { _serialized: 'wamid.synthetic.guard' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });

  assert.equal(result.status, 'blocked');
  assert.equal(listPending(config).length, 0);
  assert.equal(counters.length > 0, true);
});

test('inbound bridge rejects non-local targets even when enabled in test mode', async () => {
  const { config } = tempConfig({
    inboundForwardEnabled: true,
    inboundForwardUrl: 'http://192.0.2.6:18102/api/v1/ingress/internal/events',
  });
  const bridge = createInboundBridge(config, { info() {}, error() {}, warn() {} }, {
    fetchImpl: async () => {
      throw new Error('fetch should not be called');
    },
  });

  const result = await bridge.handleMessage({
    id: { _serialized: 'wamid.synthetic.nonlocal' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });

  assert.equal(result.status, 'blocked_invalid_target');
});

test('inbound spool survives restart and delivers exactly once', async () => {
  const { config } = tempConfig({
    inboundForwardEnabled: true,
    inboundForwardUrl: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
  });
  ensureSpool(config);
  const normalized = {
    schema_version: '1',
    source: 'wwebjs',
    external_event_id: 'wamid.synthetic.recovery',
    event_type: 'message',
    occurred_at: '2026-08-13T00:00:00.000Z',
    external_actor_id: '5500000000029@c.us',
    channel: 'whatsapp',
    message_type: 'chat',
    content: 'Fixture inbound.',
    has_media: false,
    metadata: { provider: 'wwebjs', message_type: 'chat', from_me: false, has_media: false },
    identity: {
      source_message_id: 'wamid.synthetic.recovery',
      canonical_message_id: 'wamid.synthetic.recovery',
      idempotency_key: 'wwebjs:wamid.synthetic.recovery',
      identity_hash: 'hash',
    },
  };
  const pending = enqueuePending(config, normalized);
  assert.equal(listPending(config).length, 1);

  const seenBodies = [];
  const first = await deliverPendingFile(config, pending.file, { info() {}, error() {}, warn() {} }, async (_, options) => {
    seenBodies.push(Buffer.from(options.body).toString('utf8'));
    return {
      status: 200,
      text: async () => JSON.stringify({ status: 'accepted', interaction_id: 'int-1' }),
    };
  });
  assert.equal(first.done, true);
  assert.equal(first.status, 'accepted');
  assert.equal(seenBodies.length, 1);
  assert.equal(listPending(config).length, 0);

  const second = await deliverPendingFile(config, pending.file, { info() {}, error() {}, warn() {} }, async () => {
    throw new Error('should not be called after success');
  });
  assert.equal(second.done, false);
});

test('inbound spool quarantines bad payload and keeps retryable failures pending', async () => {
  const { config } = tempConfig({
    inboundForwardEnabled: true,
    inboundForwardUrl: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
  });
  const normalized = {
    schema_version: '1',
    source: 'wwebjs',
    external_event_id: 'wamid.synthetic.retry',
    event_type: 'message',
    occurred_at: '2026-08-13T00:00:00.000Z',
    external_actor_id: '5500000000029@c.us',
    channel: 'whatsapp',
    message_type: 'chat',
    content: 'Fixture inbound.',
    has_media: false,
    metadata: { provider: 'wwebjs', message_type: 'chat', from_me: false, has_media: false },
    identity: {
      source_message_id: 'wamid.synthetic.retry',
      canonical_message_id: 'wamid.synthetic.retry',
      idempotency_key: 'wwebjs:wamid.synthetic.retry',
      identity_hash: 'hash',
    },
  };
  const pending = enqueuePending(config, normalized);

  const retry = await deliverPendingFile(config, pending.file, { info() {}, error() {}, warn() {} }, async () => ({
    status: 500,
    text: async () => '',
  }));
  assert.equal(retry.done, false);
  assert.equal(retry.status, 'retry');
  assert.equal(listPending(config).length, 1);

  const badFile = enqueuePending(config, {
    ...normalized,
    identity: { ...normalized.identity, idempotency_key: 'wwebjs:wamid.synthetic.bad' },
    external_event_id: 'wamid.synthetic.bad',
  });
  const quarantine = await deliverPendingFile(config, badFile.file, { info() {}, error() {}, warn() {} }, async () => ({
    status: 400,
    text: async () => '',
  }));
  assert.equal(quarantine.done, true);
  assert.equal(quarantine.status, 'bad_payload');
});
