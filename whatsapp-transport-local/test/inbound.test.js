const test = require('node:test');
const assert = require('node:assert/strict');

const { normalizeInboundMessage } = require('../src/inbound');

test('normalizeInboundMessage produces the local inbound contract', () => {
  const normalized = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.1' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
    fromMe: false,
    hasMedia: false,
  });

  assert.equal(normalized.schema_version, '1');
  assert.equal(normalized.source, 'wwebjs');
  assert.equal(normalized.external_event_id, 'wamid.synthetic.1');
  assert.equal(normalized.event_type, 'message');
  assert.equal(normalized.external_actor_id, '5500000000029@c.us');
  assert.equal(normalized.channel, 'whatsapp');
  assert.equal(normalized.message_type, 'chat');
  assert.equal(normalized.content, 'Fixture inbound.');
  assert.equal(normalized.has_media, false);
  assert.equal(normalized.metadata.provider, 'wwebjs');
  assert.equal(normalized.identity.source_message_id, 'wamid.synthetic.1');
  assert.equal(normalized.identity.canonical_message_id, 'wamid.synthetic.1');
  assert.equal(normalized.identity.idempotency_key, 'wwebjs:wamid.synthetic.1');
  assert.equal(typeof normalized.identity.identity_hash, 'string');
});

test('normalizeInboundMessage is stable for the same synthetic inbound', () => {
  const a = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.stable' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });
  const b = normalizeInboundMessage({
    id: { _serialized: 'wamid.synthetic.stable' },
    from: '5500000000029@c.us',
    type: 'chat',
    body: 'Fixture inbound.',
    timestamp: 1723520000,
  });

  assert.deepEqual(a.identity, b.identity);
  assert.equal(a.content, b.content);
  assert.equal(a.external_event_id, b.external_event_id);
});

test('group normalization retains participant, quote and owner text without changing direct messages', () => {
  const incoming = normalizeInboundMessage({
    id: { _serialized: 'group-event-1', remote: '12345@g.us' },
    from: '12345@g.us', author: 'participant@c.us', type: 'chat',
    body: 'O que é um agente?', fromMe: false,
    _data: { quotedStanzaID: 'prior-event', quotedParticipant: 'other@c.us' },
  });
  assert.equal(incoming.external_actor_id, '12345@g.us');
  assert.equal(incoming.metadata.group_participant_id, 'participant@c.us');
  assert.equal(incoming.metadata.quoted_message_id, 'prior-event');
  assert.equal(incoming.metadata.thread_type, 'GROUP');
  const owner = normalizeInboundMessage({
    id: { _serialized: 'group-event-2', remote: '12345@g.us' },
    from: 'owner@c.us', to: '12345@g.us', type: 'chat',
    body: 'Andy, pare.', fromMe: true,
    __finalFromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
    __fromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
  });
  assert.equal(owner.content, 'Andy, pare.');
  assert.equal(owner.external_actor_id, '12345@g.us');
  assert.equal(owner.metadata.is_group, true);
  assert.equal(owner.metadata.from_me, true);
});

test('manual owner direct text is redacted by default and opt-in is bounded', () => {
  const observed = {
    id: { _serialized: 'wamid.synthetic.owner.manual', remote: '5500000000029@c.us' },
    from: '5500000000001@c.us',
    to: '5500000000029@c.us',
    type: 'chat',
    body: 'Texto sintético do proprietário.',
    timestamp: 1794643200,
    fromMe: true,
    hasMedia: false,
    __fromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
    __finalFromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
  };
  const normal = normalizeInboundMessage(observed);
  const enabled = normalizeInboundMessage(observed, {
    conversationCaptureOwnerTextEnabled: true,
  });
  assert.equal(normal.event_origin, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(normal.content, '');
  assert.equal(enabled.event_origin, normal.event_origin);
  assert.equal(enabled.content, 'Texto sintético do proprietário.');
  assert.equal(enabled.metadata.from_me, true);
  assert.equal(enabled.metadata.conversation_state, 'READY');
  assert.equal(enabled.external_actor_id, normal.external_actor_id);

  // Unknown, automated, incomplete identity and nontext do not gain content
  // merely because the operator enabled the capture of manual text.
  const unknown = normalizeInboundMessage({
    ...observed,
    __fromMeClassification: 'UNKNOWN_FROM_ME',
    __finalFromMeClassification: 'UNKNOWN_FROM_ME',
  }, { conversationCaptureOwnerTextEnabled: true });
  assert.equal(unknown.content, '');
  const automated = normalizeInboundMessage({
    ...observed,
    __fromMeClassification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
    __finalFromMeClassification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
  }, { conversationCaptureOwnerTextEnabled: true });
  assert.equal(automated.content, '');
  const voice = normalizeInboundMessage({
    ...observed, type: 'ptt', hasMedia: true,
  }, { conversationCaptureOwnerTextEnabled: true });
  assert.equal(voice.content, '');
  const unresolved = normalizeInboundMessage({
    ...observed,
    __conversationIdentity: {
      state: 'AMBIGUOUS',
      conversation_key: null,
      peer_id: null,
      peer_identifiers: [],
    },
  }, { conversationCaptureOwnerTextEnabled: true });
  assert.equal(unresolved.content, '');
});


test('capture flag never changes incoming text or preexisting group owner wording', () => {
  const incoming = {
    id: { _serialized: 'wamid.synthetic.capture.in' },
    from: '5500000000029@c.us', type: 'chat',
    body: 'Mensagem recebida sintética.', fromMe: false,
  };
  assert.equal(normalizeInboundMessage(incoming).content, 'Mensagem recebida sintética.');
  assert.equal(normalizeInboundMessage(incoming, {
    conversationCaptureOwnerTextEnabled: true,
  }).content, 'Mensagem recebida sintética.');
  const group = {
    id: { _serialized: 'wamid.synthetic.capture.group', remote: '12345@g.us' },
    from: 'owner@c.us', to: '12345@g.us',
    type: 'chat', body: 'Texto sintético em grupo.',
    fromMe: true,
    __fromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
    __finalFromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
  };
  assert.equal(normalizeInboundMessage(group).content, 'Texto sintético em grupo.');
  assert.equal(normalizeInboundMessage(group, {
    conversationCaptureOwnerTextEnabled: true,
  }).content, 'Texto sintético em grupo.');
});
