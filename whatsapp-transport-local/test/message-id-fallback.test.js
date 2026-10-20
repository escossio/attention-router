const test = require('node:test');
const assert = require('node:assert/strict');

const { extractMessageId } = require('../src/message-id');

test('extracts a stable id when serialized message id is unavailable', () => {
  const message = {
    id: {
      fromMe: false,
      remote: { _serialized: 'fixture@c.us' },
      id: '3EB0123456789ABC',
    },
  };

  assert.equal(
    extractMessageId(message),
    'wwebjs:inbound:fixture@c.us:3EB0123456789ABC',
  );
});


test('reads the current WhatsApp Web $1 composite ID when _serialized is unavailable', () => {
  const id = { fromMe: true, remote: { _serialized: 'fixture@lid' },
    id: '3EB0SYNTHETIC', $1: 'true_fixture@lid_3EB0SYNTHETIC' };
  assert.equal(extractMessageId({ id }), id.$1);
  assert.equal(extractMessageId({ _data: { id } }), id.$1);
});
