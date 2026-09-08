'use strict';
const assert = require('node:assert/strict');
const { ACTOR_A, ACTOR_B, ACTOR_C } = require('./synthetic-identities.json');
const { canonicalPhoneIdentity, authenticatedSelfAliases, resolveWhatsAppIdentity,
  compareOwnerIdentity } = require('../../whatsapp-transport-local/src/identity');
const actors = [ACTOR_A, ACTOR_B, ACTOR_C];
for (const actor of actors) {
  assert.equal(actor.national.startsWith('00'), false);
  assert.match(actor.national, /^0190000000[123]$/);
  assert.equal(actor.e164, `+55${actor.national}`);
  assert.equal(actor.jid, `${actor.pn}@c.us`);
  for (const value of [actor.national, actor.nationalJid, actor.e164, actor.pn,
    actor.jid, actor.serverJid, actor.legacyJid, `00${actor.pn}`]) {
    assert.equal(canonicalPhoneIdentity(value), actor.e164);
  }
  assert.equal(canonicalPhoneIdentity(actor.lid), null);
  assert.notEqual(actor.lid, actor.jid);
  assert.equal(resolveWhatsAppIdentity({ raw: actor.lid }).result, 'UNRESOLVED');
  const paired = resolveWhatsAppIdentity({ raw: actor.lid, pn: actor.jid, lid: actor.lid });
  assert.equal(paired.canonicalPhone, actor.e164);
  assert.deepEqual(paired.aliases, [actor.lid, actor.jid]);
  assert.deepEqual(authenticatedSelfAliases({ raw: actor.jid, pn: actor.jid, lid: actor.lid }).aliases,
    [actor.lid, actor.jid]);
  for (const other of actors) {
    const expected = actor === other ? 'MATCH' : 'MISMATCH';
    assert.equal(compareOwnerIdentity(other.nationalJid, paired).result, expected);
  }
}
assert.deepEqual(actors.map((actor) => actor.jid).sort(), [ACTOR_A.jid, ACTOR_B.jid, ACTOR_C.jid]);
assert.deepEqual(actors.map((actor) => actor.lid).sort(), [ACTOR_A.lid, ACTOR_B.lid, ACTOR_C.lid]);
console.log('SYNTHETIC_IDENTITY_CONTRACT=PASS');
