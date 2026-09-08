const test = require('node:test');
const assert = require('node:assert/strict');

const { normalizeInboundMessage } = require('../src/inbound');

test('normalizeInboundMessage produces the local inbound contract', () => {
  const normalized = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.1' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
    fromMe: false,
    hasMedia: false,
  });

  assert.equal(normalized.schema_version, '1');
  assert.equal(normalized.source, 'wwebjs');
  assert.equal(normalized.external_event_id, 'wamid.synthetic.1');
  assert.equal(normalized.event_type, 'message');
  assert.equal(normalized.external_actor_id, '5500000000029@c.us');
  assert.equal(normalized.channel, 'whatsapp');
  assert.equal(normalized.message_type, 'chat');
  assert.equal(normalized.content, 'Fixture inbound.');
  assert.equal(normalized.has_media, false);
  assert.equal(normalized.metadata.provider, 'wwebjs');
  assert.equal(normalized.identity.source_message_id, 'wamid.synthetic.1');
  assert.equal(normalized.identity.canonical_message_id, 'wamid.synthetic.1');
  assert.equal(normalized.identity.idempotency_key, 'wwebjs:wamid.synthetic.1');
  assert.equal(typeof normalized.identity.identity_hash, 'string');
});

test('normalizeInboundMessage is stable for the same synthetic inbound', () => {
  const a = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.stable' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });
  const b = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.stable' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });

  assert.deepEqual(a.identity, b.identity);
  assert.equal(a.content, b.content);
  assert.equal(a.external_event_id, b.external_event_id);
});
