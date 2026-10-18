const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { firstContactIdentity, resolveConversationIdentity } = require('../src/conversation');
const { normalizeInboundMessage } = require('../src/inbound');
const { createConfig } = require('../src/config');
const { createStatus } = require('../src/status');
const { createOutboundProvenanceLedger } = require('../src/outbound-provenance');
const { createServer } = require('../src/server');
const { PROOF_PATH, signature } = require('../src/first-contact');

const secret = 'synthetic-first-contact-proof-secret';
const lid = '1234567890@lid';
const phone = '5511000000000@c.us';
const nonce = 'ab'.repeat(32);
const sha = (value) => crypto.createHash('sha256').update(value).digest('hex');
const scope = {
  schema_version: '1', contract_version: 'first-contact-v1',
  attempt_id: 'attempt-test', presentation_version: 'first-contact-v1',
  tenant_id: 'tenant-test', source: 'wwebjs', channel: 'whatsapp',
  source_account: 'account-test', canonical_peer_hash: sha(lid),
  execution_intent_id: 'intent-test', outbox_id: 'outbox-test',
  idempotency_key: 'execution:intent-test', text_hash: sha('Synthetic public introduction.'),
};
const payload = { external_actor_id: lid, message_type: 'text',
  text: 'Synthetic public introduction.', execution_intent_id: scope.execution_intent_id,
  outbox_id: scope.outbox_id, idempotency_key: scope.idempotency_key, first_contact: scope };

function config(extra = {}) {
  return createConfig({ FIRST_CONTACT_ENABLED: 'true', FIRST_CONTACT_TENANT_ID: scope.tenant_id,
    FIRST_CONTACT_SOURCE_ACCOUNT: scope.source_account, LOCAL_TENANT_ID: scope.tenant_id,
    LOCAL_SOURCE_ACCOUNT: scope.source_account, LOCAL_OUTBOUND_HMAC_SECRET: secret,
    EXTERNAL_DELIVERY_ENABLED: 'true', LOCAL_TRANSPORT_HTTP_PORT: '0', ...extra });
}

function request(port, body, { proof = false, headers = {}, timestamp, target } = {}) {
  const raw = Buffer.from(JSON.stringify(body));
  const stamp = timestamp || String(Math.floor(Date.now() / 1000));
  const sig = proof ? signature(secret, 'request', stamp, nonce, raw)
    : `sha256=${crypto.createHmac('sha256', secret).update(`${stamp}.${raw}`).digest('hex')}`;
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: target || (proof ? PROOF_PATH : '/internal/send'),
      method: 'POST', headers: { 'content-type': 'application/json', 'content-length': raw.length,
        'x-attention-timestamp': stamp, 'x-attention-nonce': nonce, 'x-attention-signature': sig,
        ...headers } }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const bytes = Buffer.concat(chunks);
        resolve({ code: res.statusCode, body: JSON.parse(bytes), raw: bytes, headers: res.headers });
      });
    });
    req.on('error', reject);
    req.end(raw);
  });
}

async function fixture(t, { ready = true, client = null, enabled = true } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-first-contact-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const cfg = config({ LOCAL_OUTBOUND_PROVENANCE_DIR: dir, FIRST_CONTACT_ENABLED: String(enabled) });
  const status = createStatus(cfg, ready ? { service_state: 'ready', browser_debug_reachable: true,
    wwebjs_connected: true, ready: true, client_state: 'CONNECTED' } : {});
  const ledger = createOutboundProvenanceLedger(cfg);
  const server = createServer(cfg, status, client, { outboundProvenance: ledger,
    logger: { error() {} } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return { port: server.address().port, ledger };
}

test('strict provider identity requires one directional LID and at most one matching PN', () => {
  assert.equal(firstContactIdentity(lid).provenance, 'DIRECTIONAL_LID');
  assert.equal(firstContactIdentity(phone), null);
  const mapped = firstContactIdentity(phone, [{ lid, pn: phone }]);
  assert.equal(mapped.canonical_peer_id, lid);
  assert.equal(mapped.provenance, 'PROVIDER_LID_PN_MAPPING');
  for (const mappings of [
    [{ lid, pn: '999@c.us' }], [{ lid: '999@lid', pn: phone }, { lid, pn: phone }],
    [{ lid, pn: phone }, { lid, pn: '999@c.us' }], [{ pn: phone }],
    [{ lid: 'peer@lid', pn: phone }], { lid, pn: phone }, [null],
  ]) assert.equal(firstContactIdentity(phone, mappings), null);
  assert.equal(firstContactIdentity('123@g.us'), null);
  assert.equal(firstContactIdentity('peer@lid'), null);
});

test('opt-in signal leaves legacy resolution intact and never grants group or owner identity', async () => {
  const client = { async getContactLidAndPhone() { return [{ lid, pn: phone }]; } };
  const message = { from: phone, id: { _serialized: 'synthetic-message' }, body: 'Synthetic' };
  const legacy = await resolveConversationIdentity(message, client);
  assert.equal(Object.hasOwn(legacy, 'first_contact_identity'), false);
  const verified = await resolveConversationIdentity(message, client, { firstContactEnabled: true });
  const { first_contact_identity: signal, ...oldFields } = verified;
  assert.deepEqual(oldFields, legacy);
  assert.equal(signal.canonical_peer_id, lid);
  assert.equal(normalizeInboundMessage({ ...message, __conversationIdentity: verified }).metadata
    .first_contact_identity.canonical_peer_id, lid);
  for (const change of [{ fromMe: true, to: phone }, { from: '123@g.us' }]) {
    assert.equal(Object.hasOwn(normalizeInboundMessage({ ...message, ...change,
      __conversationIdentity: verified }).metadata, 'first_contact_identity'), false);
  }
  const unrelated = { async getContactLidAndPhone() { return [{ lid: '999@lid', pn: '999@c.us' }]; } };
  const ordinary = await resolveConversationIdentity(message, unrelated);
  const denied = await resolveConversationIdentity(message, unrelated, { firstContactEnabled: true });
  assert.deepEqual(denied, ordinary);
});

test('new configuration defaults OFF and refuses incomplete or different namespace', () => {
  assert.equal(createConfig({}).firstContactEnabled, false);
  assert.throws(() => createConfig({ FIRST_CONTACT_ENABLED: 'true' }), /CONFIGURATION_INVALID/);
  assert.throws(() => config({ FIRST_CONTACT_TENANT_ID: 'other' }), /CONFIGURATION_INVALID/);
  assert.throws(() => config({ FIRST_CONTACT_SOURCE_ACCOUNT: 'other' }), /CONFIGURATION_INVALID/);
  assert.throws(() => config({ LOCAL_INBOUND_SOURCE: 'other' }), /CONFIGURATION_INVALID/);
  assert.throws(() => config({ LOCAL_INBOUND_CHANNEL: 'other' }), /CONFIGURATION_INVALID/);
});

test('ledger binds full FC scope and monotonic ACK while legacy fingerprint stays compatible', async (t) => {
  const { ledger } = await fixture(t);
  const first = ledger.begin({ ...payload, destination: lid });
  assert.equal(first.replay, false);
  assert.equal(ledger.begin({ ...payload, destination: lid }).replay, true);
  const reordered = Object.fromEntries(Object.entries(scope).reverse());
  assert.equal(ledger.begin({ ...payload, destination: lid, first_contact: reordered }).replay, true);
  assert.throws(() => ledger.begin({ ...payload, destination: lid,
    first_contact: { ...scope, attempt_id: 'other' } }), /IDEMPOTENCY_CONFLICT/);
  assert.throws(() => ledger.begin({ ...payload, destination: lid,
    execution_intent_id: 'other', idempotency_key: 'execution:other' }), /PROVENANCE_INVALID/);
  ledger.markSent(first.record, 'synthetic-reference', 1);
  ledger.markAck('synthetic-reference', 3);
  ledger.markAck('synthetic-reference', 2);
  assert.equal(ledger.statusForKey(scope.idempotency_key).ack, 3);
  const oldPayload = { idempotency_key: 'legacy-key', destination: phone, text: 'Legacy' };
  const old = ledger.begin(oldPayload).record;
  const oldFingerprint = sha(JSON.stringify({ idempotency_key: 'legacy-key',
    conversation_key: `wwebjs:${phone}`, peer_identifiers: [phone], text_hash: sha('Legacy'),
    message_type: 'text', artifact_sha256: null, mime_type: null, size_bytes: null }));
  assert.equal(old.fingerprint, oldFingerprint);
  assert.equal(Object.hasOwn(old, 'first_contact'), false);
});

test('proof endpoint is flag-gated, authenticated, fresh and ledger-only even browser offline', async (t) => {
  const off = await fixture(t, { enabled: false });
  assert.equal((await request(off.port, scope, { proof: true })).code, 404);
  const { port, ledger } = await fixture(t, { ready: false, client: {
    sendMessage() { assert.fail('proof must never send'); },
    getContactLidAndPhone() { assert.fail('proof must never query provider'); },
  } });
  assert.equal((await request(port, scope, { proof: true,
    headers: { 'x-attention-signature': 'sha256=' + '00'.repeat(32) } })).code, 401);
  assert.equal((await request(port, scope, { proof: true,
    timestamp: String(Math.floor(Date.now() / 1000) - 61) })).code, 401);
  assert.equal((await request(port, { ...scope, tenant_id: 'other' }, { proof: true })).code, 403);
  const unknown = await request(port, scope, { proof: true });
  assert.equal(unknown.code, 200);
  assert.equal(unknown.body.status, 'UNKNOWN');
  assert.equal(unknown.body.ack, null);
  assert.equal(unknown.headers['x-attention-proof-signature'], signature(secret, 'response',
    unknown.headers['x-attention-proof-timestamp'], nonce, unknown.raw));
  const started = ledger.begin({ ...payload, destination: lid });
  ledger.markSent(started.record, 'synthetic-reference', 2);
  const proof = await request(port, scope, { proof: true });
  assert.equal(proof.code, 200);
  assert.equal(proof.body.ack, 2);
  assert.equal(proof.body.message_reference, 'synthetic-reference');
  for (const field of Object.keys(scope)) {
    const changed = { ...scope, [field]: field.endsWith('_hash') ? 'ff'.repeat(32) : `${scope[field]}-different` };
    const response = await request(port, changed, { proof: true });
    assert.ok([403, 409].includes(response.code), field);
  }
  assert.equal((await request(port, scope, { proof: true, target: `${PROOF_PATH}?extra=1` })).code, 405);
});

test('FC send compares canonical provider peer, full scope, durable replay and ACK', async (t) => {
  let sends = 0;
  const { port, ledger } = await fixture(t, { client: { async sendMessage() {
    sends += 1; return { id: { _serialized: 'synthetic-reference' }, ack: 1 };
  } } });
  assert.equal((await request(port, { ...payload, first_contact: { ...scope, text_hash: 'ff'.repeat(32) } })).code, 403);
  assert.equal((await request(port, { ...payload, execution_intent_id: 'other' })).code, 403);
  assert.equal((await request(port, { ...payload, external_actor_id: phone })).code, 403);
  assert.equal((await request(port, { ...payload, external_actor_id: '123@g.us' })).code, 403);
  assert.equal(sends, 0);
  assert.equal((await request(port, payload)).body.status, 'sent');
  assert.equal((await request(port, payload)).body.status, 'already_sent');
  assert.equal((await request(port, { ...payload,
    first_contact: { ...scope, attempt_id: 'other' } })).code, 409);
  assert.equal(sends, 1);
  ledger.markAck('synthetic-reference', 2);
  assert.equal((await request(port, scope, { proof: true })).body.ack, 2);
});

test('read-only proof of UNKNOWN never creates a missing provenance directory', async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-first-contact-read-only-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const directory = path.join(parent, 'never-created');
  const cfg = config({ LOCAL_OUTBOUND_PROVENANCE_DIR: directory });
  const server = createServer(cfg, createStatus(cfg));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const proof = await request(server.address().port, scope, { proof: true });
  assert.equal(proof.code, 200);
  assert.equal(proof.body.status, 'UNKNOWN');
  assert.equal(fs.existsSync(directory), false);
});

test('unknown send result stays blocked on retry and synthetic references never prove delivery', async (t) => {
  let sends = 0;
  const { port } = await fixture(t, { client: { async sendMessage() { sends += 1; return {}; } } });
  assert.equal((await request(port, payload)).code, 503);
  assert.equal((await request(port, payload)).code, 409);
  assert.equal(sends, 1);
  const proof = await request(port, scope, { proof: true });
  assert.equal(proof.body.status, 'FAILED');
  assert.equal(proof.body.message_reference, null);
  assert.equal(proof.body.ack, null);
});
