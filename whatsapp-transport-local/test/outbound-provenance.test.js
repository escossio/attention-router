const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { identityFromAliases } = require('../src/conversation');
const { createOutboundProvenanceLedger } = require('../src/outbound-provenance');
const { classifyFromMeMessage } = require('../src/transport');

function ledgerFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-outbound-provenance-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, ledger: createOutboundProvenanceLedger({ outboundProvenanceDir: directory }) };
}

test('outbound provenance is durable and idempotent', (t) => {
  const { directory, ledger } = ledgerFixture(t);
  const payload = {
    idempotency_key: 'execution:intent-1',
    execution_intent_id: 'intent-1',
    outbox_id: 'outbox-1',
    destination: 'peer@lid',
    text: 'Fixture automatic response.',
  };
  const first = ledger.begin(payload);
  assert.equal(first.replay, false);
  assert.equal(first.record.status, 'SENDING');
  ledger.markSent(first.record, 'message-1');

  const reopened = createOutboundProvenanceLedger({ outboundProvenanceDir: directory });
  assert.equal(reopened.begin(payload).record.status, 'SENT');
  assert.equal(reopened.begin(payload).replay, true);
});

test('message_create before sendMessage return is classified as Router automated', async (t) => {
  const { ledger } = ledgerFixture(t);
  ledger.begin({
    idempotency_key: 'execution:race',
    execution_intent_id: 'intent-race',
    outbox_id: 'outbox-race',
    destination: 'peer@lid',
    text: 'Fixture automatic response.',
    conversation_identity: identityFromAliases(['peer@lid', '5500000000025@c.us']),
  });
  const message = {
    id: { _serialized: 'message-race' },
    fromMe: true,
    from: 'owner@c.us',
    to: '5500000000025@c.us',
    body: 'Fixture automatic response.',
  };
  const client = {
    getContactLidAndPhone: async () => [{ lid: 'peer@lid', pn: '5500000000025@c.us' }],
  };
  const result = await classifyFromMeMessage(message, client, ledger);
  assert.equal(result.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
  assert.equal(result.reason, 'UNIQUE_INFLIGHT_PROVENANCE_MATCH');
});

test('failed outbound provenance fails closed when a matching message_create arrives later', async (t) => {
  const { ledger } = ledgerFixture(t);
  const begun = ledger.begin({
    idempotency_key: 'execution:failed',
    execution_intent_id: 'intent-failed',
    outbox_id: 'outbox-failed',
    destination: 'peer@lid',
    text: 'Possibly accepted before failure.',
    conversation_identity: identityFromAliases(['peer@lid', '5500000000025@c.us']),
  });
  ledger.markFailed(begun.record, 'TransportError');

  const result = await classifyFromMeMessage({
    id: { _serialized: 'different-message-reference' },
    fromMe: true,
    from: 'owner@c.us',
    to: '5500000000025@c.us',
    body: 'Possibly accepted before failure.',
  }, {
    getContactLidAndPhone: async () => [{ lid: 'peer@lid', pn: '5500000000025@c.us' }],
  }, ledger);

  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.notEqual(result.classification, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(result.reason, 'FAILED_OUTBOUND_PROVENANCE_MATCH');
});

test('sent outbound provenance remains classified as Router automated', async (t) => {
  const { ledger } = ledgerFixture(t);
  const begun = ledger.begin({
    idempotency_key: 'execution:sent',
    destination: 'peer@lid',
    text: 'Confirmed automatic response.',
    conversation_identity: identityFromAliases(['peer@lid', '5500000000025@c.us']),
  });
  ledger.markSent(begun.record, 'sent-message-reference');

  const result = await classifyFromMeMessage({
    id: { _serialized: 'observed-message-reference' },
    fromMe: true,
    to: 'peer@lid',
    body: 'Confirmed automatic response.',
  }, {
    getContactLidAndPhone: async () => [{ lid: 'peer@lid', pn: '5500000000025@c.us' }],
  }, ledger);

  assert.equal(result.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
  assert.equal(result.reason, 'UNIQUE_INFLIGHT_PROVENANCE_MATCH');
});

test('unmatched fromMe is manual and ambiguous correlation fails closed', async (t) => {
  const { ledger } = ledgerFixture(t);
  const client = {
    getContactLidAndPhone: async () => [{ lid: 'peer@lid', pn: '5500000000025@c.us' }],
  };
  const manual = await classifyFromMeMessage({
    id: { _serialized: 'manual-1' }, fromMe: true, to: 'peer@lid', body: 'Manual fixture.',
  }, client, ledger);
  assert.equal(manual.classification, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(manual.reason, 'NO_ROUTER_OUTBOUND_MATCH');

  for (const suffix of ['a', 'b']) {
    ledger.begin({
      idempotency_key: `execution:${suffix}`,
      destination: 'peer@lid',
      text: 'Same fixture.',
      conversation_identity: identityFromAliases(['peer@lid', '5500000000025@c.us']),
    });
  }
  const ambiguous = await classifyFromMeMessage({
    id: { _serialized: 'ambiguous-1' }, fromMe: true, to: 'peer@lid', body: 'Same fixture.',
  }, client, ledger);
  assert.equal(ambiguous.classification, 'UNKNOWN_FROM_ME');
  assert.equal(ambiguous.reason, 'MULTIPLE_OUTBOUND_PROVENANCE_MATCHES');
});

test('expired fingerprint fails closed instead of becoming a manual owner message', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-outbound-provenance-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let now = new Date('2026-09-04T12:00:00Z');
  const ledger = createOutboundProvenanceLedger(
    { outboundProvenanceDir: directory, outboundProvenanceCorrelationMs: 120000 },
    { now: () => now },
  );
  const begun = ledger.begin({
    idempotency_key: 'execution:expired',
    destination: 'peer@lid',
    text: 'Same words later typed manually.',
    conversation_identity: identityFromAliases(['peer@lid']),
  });
  ledger.markSent(begun.record, 'router-message-reference');
  now = new Date('2026-09-04T12:02:01Z');
  const result = ledger.classify(
    { id: { _serialized: 'manual-reference' }, body: 'Same words later typed manually.' },
    identityFromAliases(['peer@lid']),
  );
  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.equal(result.reason, 'STALE_OUTBOUND_PROVENANCE_MATCH');
});

test('expired failed provenance also remains fail closed', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-outbound-provenance-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let now = new Date('2026-09-04T12:00:00Z');
  const ledger = createOutboundProvenanceLedger(
    { outboundProvenanceDir: directory, outboundProvenanceCorrelationMs: 120000 },
    { now: () => now },
  );
  const begun = ledger.begin({
    idempotency_key: 'execution:expired-failed',
    destination: 'peer@lid',
    text: 'Uncertain automatic response.',
    conversation_identity: identityFromAliases(['peer@lid']),
  });
  ledger.markFailed(begun.record, 'TransportError');
  now = new Date('2026-09-04T12:02:01Z');

  const result = ledger.classify(
    { id: { _serialized: 'later-message-reference' }, body: 'Uncertain automatic response.' },
    identityFromAliases(['peer@lid']),
  );

  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.equal(result.reason, 'STALE_OUTBOUND_PROVENANCE_MATCH');
});

test('media race requires exact sendMessage reference before automated classification', async (t) => {
  const { ledger } = ledgerFixture(t);
  const begun = ledger.begin({
    idempotency_key: 'execution:voice-race', execution_intent_id: 'intent-voice',
    outbox_id: 'outbox-voice', destination: 'peer@lid', message_type: 'ptt',
    content_sha256: 'a'.repeat(64), mime_type: 'audio/mpeg', size_bytes: 10,
    conversation_identity: identityFromAliases(['peer@lid']),
  });
  const message = { id: { _serialized: 'voice-message-reference' }, fromMe: true,
    to: 'peer@lid', type: 'ptt', hasMedia: true, body: '' };
  const pending = ledger.classify(message, identityFromAliases(['peer@lid']));
  assert.equal(pending.classification, 'UNKNOWN_FROM_ME');
  assert.equal(pending.reason, 'MEDIA_REFERENCE_PENDING');
  setTimeout(() => ledger.markSent(begun.record, 'voice-message-reference'), 20);
  const exact = await ledger.classifyBounded(message, identityFromAliases(['peer@lid']));
  assert.equal(exact.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
  assert.equal(exact.reason, 'MESSAGE_REFERENCE_MATCH');
});

test('unmatched media fromMe is UNKNOWN_FROM_ME, never manual', async (t) => {
  const { ledger } = ledgerFixture(t);
  const result = await ledger.classifyBounded({
    id: { _serialized: 'unmatched-media' }, fromMe: true, to: 'peer@lid',
    type: 'ptt', hasMedia: true, body: '',
  }, identityFromAliases(['peer@lid']));
  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.equal(result.reason, 'UNMATCHED_MEDIA_FROM_ME');
});
