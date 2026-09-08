const crypto = require('node:crypto');

function signBody({ secret, timestamp, body }) {
  const ts = String(timestamp);
  const bodyBuffer = Buffer.isBuffer(body) ? body : Buffer.from(body);
  return `sha256=${crypto.createHmac('sha256', Buffer.from(secret)).update(Buffer.concat([Buffer.from(`${ts}.`), bodyBuffer])).digest('hex')}`;
}

function headersForBody({ secret, body, now = Date.now }) {
  const timestamp = Math.floor(now() / 1000).toString();
  return {
    'Content-Type': 'application/json',
    'X-Attention-Timestamp': timestamp,
    'X-Attention-Signature': signBody({ secret, timestamp, body }),
  };
}

function stableHash(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

function timingSafeEqualString(a, b) {
  const left = Buffer.from(String(a || ''));
  const right = Buffer.from(String(b || ''));
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

module.exports = { signBody, headersForBody, stableHash, timingSafeEqualString };
