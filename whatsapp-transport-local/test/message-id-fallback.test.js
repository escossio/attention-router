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
