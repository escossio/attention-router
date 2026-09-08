const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const { headersForBody, signBody } = require('../src/hmac');

test('HMAC signing matches the documented byte contract', () => {
  const secret = 'unit-test-inbound-secret-32-bytes-minimum';
  const timestamp = '1723520000';
  const body = Buffer.from('{"schema_version":"1","source":"wwebjs"}');

  const signature = signBody({ secret, timestamp, body });
  const expected = crypto
    .createHmac('sha256', Buffer.from(secret))
    .update(Buffer.concat([Buffer.from(`${timestamp}.`), body]))
    .digest('hex');

  assert.equal(signature, `sha256=${expected}`);
});

test('HMAC headers use the exact timestamp-body prefix', () => {
  const secret = 'unit-test-inbound-secret-32-bytes-minimum';
  const body = Buffer.from('{"a":1}');
  const headers = headersForBody({
    secret,
    body,
    now: () => 1723520000000,
  });

  assert.equal(headers['X-Attention-Timestamp'], '1723520000');
  assert.match(headers['X-Attention-Signature'], /^sha256=/);
  assert.equal(headers['Content-Type'], 'application/json');
});
