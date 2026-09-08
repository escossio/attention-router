const test = require('node:test');
const assert = require('node:assert/strict');

const { createInboundBridge } = require('../src/bridge');

test('bridge updates sanitized inbound observability counters', async () => {
  const counters = [];
  const bridge = createInboundBridge(
    {
      inboundForwardEnabled: false,
      inboundForwardUrl: null,
      inboundSource: 'wwebjs',
      inboundChannel: 'whatsapp',
      hmacSecret: 'unit-test-inbound-secret-32-bytes-minimum',
      inboundHttpTimeoutMs: 100,
    },
    { info() {}, error() {}, warn() {} },
    {
      onCounters: (value) => counters.push(value),
    },
  );

  const result = await bridge.handleMessage({
    id: { _serialized: 'wamid.synthetic.observe' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });

  assert.equal(result.status, 'blocked');
  assert.equal(counters.length >= 1, true);
  assert.equal(Object.prototype.hasOwnProperty.call(result.normalized, 'content'), true);
});

test('bridge propagates authenticated self aliases to normalization', async () => {
  const bridge = createInboundBridge(
    {
      inboundForwardEnabled: false,
      inboundForwardUrl: null,
      inboundSource: 'wwebjs',
      inboundChannel: 'whatsapp',
      hmacSecret: 'unit-test-inbound-secret-32-bytes-minimum',
      inboundHttpTimeoutMs: 100,
    },
    { info() {}, error() {}, warn() {} },
  );
  const result = await bridge.handleMessage({
    id: { _serialized: 'owner-self-chat-pn-lid', remote: 'owner@lid' },
    fromMe: true,
    from: 'owner@c.us',
    to: 'owner@lid',
    type: 'chat',
    body: 'status',
    __finalFromMeClassification: 'OWNER_COMMAND',
    __fromMeClassification: 'OWNER_COMMAND',
    __ownerSelfChat: true,
    __authenticatedOwnerSelfChatTarget: true,
  }, {
    clientInfo: { wid: 'owner@c.us' },
    authenticatedSelfIdentity: {
      wid: 'owner@c.us',
      pn: 'owner@c.us',
      lid: 'owner@lid',
      aliases: ['owner@c.us', 'owner@lid'],
      ownerVerified: true,
    },
    authenticatedSelfAuthorityCurrent: true,
  });

  assert.equal(result.status, 'blocked');
  assert.equal(result.normalized.event_origin, 'OWNER_COMMAND');
  assert.equal(result.normalized.owner_authenticated, true);
  assert.equal(result.normalized.metadata.owner_self_chat, true);
});

test('bridge propagates stale authority as fail-closed to normalization', async () => {
  const bridge = createInboundBridge(
    {
      inboundForwardEnabled: false,
      inboundForwardUrl: null,
      inboundSource: 'wwebjs',
      inboundChannel: 'whatsapp',
      hmacSecret: 'unit-test-inbound-secret-32-bytes-minimum',
      inboundHttpTimeoutMs: 100,
    },
    { info() {}, error() {}, warn() {} },
  );
  const result = await bridge.handleMessage({
    id: { _serialized: 'stale-owner-self-chat', remote: 'owner@lid' },
    fromMe: true,
    from: 'owner@c.us',
    to: 'owner@lid',
    type: 'chat',
    body: 'status',
    __finalFromMeClassification: 'OWNER_COMMAND',
    __fromMeClassification: 'OWNER_COMMAND',
    __ownerSelfChat: true,
  }, {
    clientInfo: { wid: 'owner@c.us' },
    authenticatedSelfIdentity: {
      wid: 'owner@c.us',
      pn: 'owner@c.us',
      lid: 'owner@lid',
      aliases: ['owner@c.us', 'owner@lid'],
      ownerVerified: true,
    },
    authenticatedSelfAuthorityCurrent: false,
  });

  assert.equal(result.normalized.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(result.normalized.owner_authenticated, false);
  assert.equal(result.normalized.metadata.owner_self_chat, false);
  assert.equal(result.normalized.metadata.final_from_me_classification, 'UNKNOWN_FROM_ME');
});
