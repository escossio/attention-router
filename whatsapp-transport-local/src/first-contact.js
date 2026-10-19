const crypto = require('node:crypto');

const PROOF_PATH = '/internal/first-contact/delivery';
const DOMAIN = 'first-contact-delivery:v1';
const FC_FIELDS = ['attempt_id', 'canonical_peer_hash', 'contract_version',
  'presentation_version', 'source_account', 'tenant_id'];
const SCOPE_FIELDS = [...FC_FIELDS, 'schema_version', 'source', 'channel',
  'execution_intent_id', 'outbox_id', 'idempotency_key', 'text_hash'];

function sameKeys(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function bounded(value, limit = 180) {
  return typeof value === 'string' && value.length > 0 && value.length <= limit
    && value === value.trim() && !/[\x00-\x1f\x7f]/.test(value);
}

function validFirstContact(value) {
  return validScope(value);
}

function validScope(value) {
  return sameKeys(value, SCOPE_FIELDS) && value.contract_version === 'first-contact-v1'
    && ['attempt_id', 'presentation_version', 'source_account', 'tenant_id']
      .every((key) => bounded(value[key]))
    && typeof value.canonical_peer_hash === 'string'
    && /^[0-9a-f]{64}$/.test(value.canonical_peer_hash)
    && value.schema_version === '1' && value.source === 'wwebjs' && value.channel === 'whatsapp'
    && ['execution_intent_id', 'outbox_id', 'idempotency_key'].every((key) => bounded(value[key]))
    && value.idempotency_key === `execution:${value.execution_intent_id}`
    && typeof value.text_hash === 'string' && /^[0-9a-f]{64}$/.test(value.text_hash);
}

function matchesConfiguredScope(value, config) {
  const mode = config.firstContactEligibilityMode || 'INDIVIDUAL';
  const peerAllowed = mode === 'OPEN_INBOUND'
    ? !config.firstContactCanaryPeerSha256
    : mode === 'INDIVIDUAL'
      && typeof config.firstContactCanaryPeerSha256 === 'string'
      && /^[0-9a-f]{64}$/.test(config.firstContactCanaryPeerSha256)
      && value.canonical_peer_hash === config.firstContactCanaryPeerSha256;
  return config.firstContactEnabled === true
    && ['INDIVIDUAL', 'OPEN_INBOUND'].includes(mode)
    && bounded(config.firstContactTenantId) && bounded(config.firstContactSourceAccount)
    && peerAllowed
    && value.tenant_id === config.firstContactTenantId && value.tenant_id === config.tenantId
    && value.source_account === config.firstContactSourceAccount
    && value.source_account === config.sourceAccount;
}

function signature(secret, direction, timestamp, nonce, body) {
  return `sha256=${crypto.createHmac('sha256', secret).update(Buffer.concat([
    Buffer.from(`${DOMAIN}\n${direction}\n${timestamp}\n${nonce}\n`), body,
  ])).digest('hex')}`;
}

function validRequest(req, body, secret, now) {
  const timestamp = req.headers['x-attention-timestamp'];
  const nonce = req.headers['x-attention-nonce'];
  const supplied = req.headers['x-attention-signature'];
  if (!secret || typeof timestamp !== 'string' || !/^[0-9]{1,12}$/.test(timestamp)
      || typeof nonce !== 'string' || !/^[0-9a-f]{64}$/.test(nonce)
      || typeof supplied !== 'string' || !/^sha256=[0-9a-f]{64}$/.test(supplied)) return false;
  const age = Math.floor(now() / 1000) - Number(timestamp);
  if (age < -5 || age > 60) return false;
  return crypto.timingSafeEqual(Buffer.from(supplied),
    Buffer.from(signature(secret, 'request', timestamp, nonce, body)));
}

function scopeForRecord(record) {
  if (!validFirstContact(record?.first_contact)) return null;
  const scope = record.first_contact;
  return ['execution_intent_id', 'outbox_id', 'idempotency_key', 'text_hash']
    .every((key) => scope[key] === record[key]) ? scope : null;
}

module.exports = { PROOF_PATH, FC_FIELDS, bounded, validFirstContact, validScope,
  matchesConfiguredScope, signature, validRequest, scopeForRecord };
