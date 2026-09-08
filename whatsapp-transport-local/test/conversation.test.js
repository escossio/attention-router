const test = require('node:test');
const assert = require('node:assert/strict');

const {
  directionalPeerId,
  identityFromAliases,
  resolveConversationIdentity,
} = require('../src/conversation');

test('conversation peer is directional for inbound and fromMe messages', () => {
  assert.equal(directionalPeerId({ fromMe: false, from: 'peer@lid', to: 'owner@c.us' }), 'peer@lid');
  assert.equal(directionalPeerId({ fromMe: true, from: 'owner@c.us', to: 'peer@lid' }), 'peer@lid');
  assert.equal(directionalPeerId({ fromMe: false, id: { remote: 'peer@c.us' } }), 'peer@c.us');
  assert.equal(directionalPeerId({ fromMe: true, id: { remote: 'peer@c.us' } }), 'peer@c.us');
});

test('LID is the canonical key when LID and c.us aliases are known', () => {
  const fromLid = identityFromAliases(['peer@lid', '5500000000025@c.us']);
  const fromPhone = identityFromAliases(['5500000000025@c.us', 'peer@lid']);
  assert.equal(fromLid.state, 'READY');
  assert.equal(fromLid.conversation_key, 'wwebjs:peer@lid');
  assert.equal(fromPhone.conversation_key, fromLid.conversation_key);
  assert.deepEqual(fromPhone.peer_identifiers, fromLid.peer_identifiers);
});

test('provider alias mapping yields the same key from LID and c.us', async () => {
  const client = {
    getContactLidAndPhone: async () => [{ lid: 'peer@lid', pn: '5500000000025@c.us' }],
  };
  const inbound = await resolveConversationIdentity({ fromMe: false, from: 'peer@lid' }, client);
  const ownerOutbound = await resolveConversationIdentity({ fromMe: true, to: '5500000000025@c.us' }, client);
  assert.equal(inbound.conversation_key, ownerOutbound.conversation_key);
});

test('missing and conflicting unmapped peers fail closed', () => {
  assert.equal(identityFromAliases([]).state, 'AMBIGUOUS');
  assert.equal(identityFromAliases(['opaque-a', 'opaque-b']).state, 'AMBIGUOUS');
});

test('self-chat remains identifiable without treating owner as a peer alias', async () => {
  const identity = await resolveConversationIdentity(
    { fromMe: true, from: 'owner@c.us', to: 'owner@c.us' },
    null,
  );
  assert.equal(identity.conversation_key, 'wwebjs:owner@c.us');
});
