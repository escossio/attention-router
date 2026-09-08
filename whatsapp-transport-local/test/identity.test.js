// Shared synthetic identities preserve PN/JID equivalence and explicit alias order.
const { ACTOR_A, ACTOR_B, ACTOR_C } = require('../../tests/fixtures/synthetic-identities.json');
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  authenticatedSelfAliases,
  canonicalPhoneIdentity,
  compareOwnerIdentity,
  resolveWhatsAppIdentity,
} = require('../src/identity');

test('normalizes Brazilian phone, E.164, PN and serialized phone JIDs', () => {
  const expected = ACTOR_A.e164;
  for (const value of [ACTOR_A.national, ACTOR_A.e164, ACTOR_A.jid, ACTOR_A.serverJid]) {
    assert.equal(canonicalPhoneIdentity(value), expected);
  }
});

test('resolves a PN self identity and compares it to a local Brazilian owner reference', () => {
  const identity = resolveWhatsAppIdentity({ raw: ACTOR_A.jid });
  assert.equal(identity.result, 'RESOLVED');
  assert.equal(identity.canonicalPhone, ACTOR_A.e164);
  assert.equal(compareOwnerIdentity(ACTOR_A.nationalJid, identity).result, 'MATCH');
});

test('does not turn LID digits into a phone number', () => {
  const identity = resolveWhatsAppIdentity({ raw: ACTOR_A.lid, lid: ACTOR_A.lid });
  assert.equal(identity.result, 'UNRESOLVED');
  assert.equal(compareOwnerIdentity(ACTOR_A.e164, identity).result, 'UNRESOLVED');
});

test('authoritative PN resolves a paired LID without accepting unrelated identities', () => {
  const owner = resolveWhatsAppIdentity({ raw: ACTOR_A.lid, pn: ACTOR_A.jid, lid: ACTOR_A.lid });
  assert.equal(owner.canonicalPhone, ACTOR_A.e164);
  assert.equal(owner.pn, ACTOR_A.jid);
  assert.equal(owner.lid, ACTOR_A.lid);
  assert.deepEqual(owner.aliases, [ACTOR_A.lid, ACTOR_A.jid]);
  assert.equal(compareOwnerIdentity(ACTOR_A.e164, owner).result, 'MATCH');
  const other = resolveWhatsAppIdentity({ raw: ACTOR_C.jid });
  assert.equal(compareOwnerIdentity(ACTOR_A.e164, other).result, 'MISMATCH');
});

test('synthetic PN is never classified as the Owner', () => {
  const synthetic = resolveWhatsAppIdentity({ raw: ACTOR_B.jid });
  assert.equal(compareOwnerIdentity(ACTOR_A.e164, synthetic).result, 'MISMATCH');
});

test('authenticated self aliases preserve only complete exact PN and LID identifiers', () => {
  const identity = authenticatedSelfAliases({
    raw: ACTOR_A.jid,
    pn: ACTOR_A.jid,
    lid: ACTOR_A.lid,
  });
  assert.equal(identity.wid, ACTOR_A.jid);
  assert.equal(identity.pn, ACTOR_A.jid);
  assert.equal(identity.lid, ACTOR_A.lid);
  assert.deepEqual(identity.aliases, [ACTOR_A.lid, ACTOR_A.jid]);

  const wrongKinds = authenticatedSelfAliases({
    raw: 'opaque',
    pn: ACTOR_A.lid,
    lid: ACTOR_A.jid,
  });
  assert.equal(wrongKinds.pn, null);
  assert.equal(wrongKinds.lid, null);
  assert.deepEqual(wrongKinds.aliases, []);
});
