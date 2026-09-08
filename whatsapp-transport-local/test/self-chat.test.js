const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');

const { normalizeInboundMessage } = require('../src/inbound');
const { isAuthenticatedOwnerSelfChatMessage } = require('../src/message-id');
const { createOutboundProvenanceLedger } = require('../src/outbound-provenance');
const {
  classifyFinalFromMeMessage,
  classifyFromMeMessage,
  createAuthenticatedSelfAuthorityState,
} = require('../src/transport');

const OWNER = 'owner@c.us';
const OWNER_LID = 'owner@lid';
const CONTACT_A_LID = 'morgan@lid';
const EXTERNAL_PN = 'external@c.us';
const AUTHENTICATED_OWNER = Object.freeze({
  result: 'RESOLVED',
  wid: OWNER,
  pn: OWNER,
  lid: OWNER_LID,
  aliases: Object.freeze([OWNER, OWNER_LID]),
  ownerVerified: true,
});
const CURRENT_AUTHORITY = Object.freeze({ isCurrent: () => true });

function authorityPage() {
  const page = new EventEmitter();
  const frame = {};
  page.mainFrame = () => frame;
  return page;
}

function ledgerFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-self-chat-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return createOutboundProvenanceLedger({ outboundProvenanceDir: directory });
}

function realCanaryShape() {
  return {
    id: { _serialized: 'manual-canary-message', remote: CONTACT_A_LID },
    chatId: CONTACT_A_LID,
    fromMe: true,
    from: OWNER,
    to: CONTACT_A_LID,
    body: 'Manual owner reply fixture.',
    type: 'chat',
    timestamp: 1788557323,
    hasMedia: false,
  };
}

function ownerSelfChatShape(overrides = {}) {
  return {
    id: { _serialized: 'owner-self-chat', remote: OWNER },
    chatId: OWNER,
    fromMe: true,
    from: OWNER,
    to: OWNER,
    body: 'status',
    type: 'chat',
    ...overrides,
  };
}

function ownerPnToLidSelfChatShape(overrides = {}) {
  return ownerSelfChatShape({
    id: { _serialized: 'owner-self-chat-pn-lid', remote: OWNER_LID },
    chatId: OWNER_LID,
    from: OWNER,
    to: OWNER_LID,
    ...overrides,
  });
}

async function normalizeAfterFinalClassification(
  message,
  client,
  ledger,
  authenticatedSelfIdentity = AUTHENTICATED_OWNER,
  authorityCurrent = true,
) {
  const classified = await classifyFinalFromMeMessage(
    message,
    client,
    ledger,
    authenticatedSelfIdentity,
    { isCurrent: () => authorityCurrent },
  );
  message.__finalFromMeClassification = classified.classification;
  message.__fromMeClassification = classified.classification;
  message.__ownerSelfChat = classified.classification === 'OWNER_COMMAND';
  message.__authenticatedOwnerSelfChatTarget = classified.authenticatedOwnerSelfChat;
  message.__outboundProvenance = classified.provenance;
  return {
    classified,
    normalized: normalizeInboundMessage(message, {
      clientInfo: client.info,
      authenticatedSelfIdentity,
      authenticatedSelfAuthorityCurrent: authorityCurrent,
    }),
  };
}

test('real Canary A shape is normal owner outbound, not authenticated self-chat', async (t) => {
  const message = realCanaryShape();
  const client = { info: { wid: OWNER } };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), false);

  const classified = await classifyFromMeMessage(
    message,
    client,
    ledgerFixture(t),
    AUTHENTICATED_OWNER,
    CURRENT_AUTHORITY,
  );
  assert.equal(classified.classification, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(classified.reason, 'NO_ROUTER_OUTBOUND_MATCH');

  message.__fromMeClassification = classified.classification;
  const normalized = normalizeInboundMessage(message, {
    clientInfo: client.info,
    authenticatedSelfIdentity: AUTHENTICATED_OWNER,
    authenticatedSelfAuthorityCurrent: true,
  });
  assert.equal(normalized.event_origin, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(normalized.owner_authenticated, false);
  assert.equal(normalized.metadata.owner_self_chat, false);
});

test('manual authenticated owner self-chat becomes Owner Command only after provenance classification', async (t) => {
  const message = ownerSelfChatShape();
  const client = { info: { wid: OWNER } };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), true);
  const beforeClassification = normalizeInboundMessage(message, {
    clientInfo: client.info,
    authenticatedSelfIdentity: AUTHENTICATED_OWNER,
    authenticatedSelfAuthorityCurrent: true,
  });
  assert.notEqual(beforeClassification.event_origin, 'OWNER_COMMAND');

  const { classified, normalized } = await normalizeAfterFinalClassification(
    message,
    client,
    ledgerFixture(t),
  );
  assert.equal(classified.classification, 'OWNER_COMMAND');
  assert.equal(normalized.event_origin, 'OWNER_COMMAND');
  assert.equal(normalized.owner_authenticated, true);
  assert.equal(normalized.metadata.owner_self_chat, true);
  assert.equal(normalized.metadata.final_from_me_classification, 'OWNER_COMMAND');
});

test('real PN to LID owner shape is an exact authenticated self-chat', async (t) => {
  const message = ownerPnToLidSelfChatShape();
  const client = { info: { wid: OWNER } };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), true);
  const { classified, normalized } = await normalizeAfterFinalClassification(
    message,
    client,
    ledgerFixture(t),
  );

  assert.equal(classified.classification, 'OWNER_COMMAND');
  assert.equal(normalized.event_origin, 'OWNER_COMMAND');
  assert.equal(normalized.owner_authenticated, true);
  assert.equal(normalized.metadata.owner_self_chat, true);
  assert.equal(normalized.metadata.from_me_classification, 'OWNER_COMMAND');
  assert.equal(normalized.metadata.final_from_me_classification, 'OWNER_COMMAND');
});

test('normalization downgrades an Owner Command marker without authenticated aliases', () => {
  const message = ownerPnToLidSelfChatShape({
    __finalFromMeClassification: 'OWNER_COMMAND',
    __fromMeClassification: 'OWNER_COMMAND',
    __ownerSelfChat: true,
    __authenticatedOwnerSelfChatTarget: true,
  });
  const normalized = normalizeInboundMessage(message, {
    clientInfo: { wid: OWNER },
    authenticatedSelfIdentity: { ...AUTHENTICATED_OWNER, aliases: [OWNER] },
    authenticatedSelfAuthorityCurrent: true,
  });

  assert.equal(normalized.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(normalized.owner_authenticated, false);
  assert.equal(normalized.metadata.owner_self_chat, false);
  assert.equal(normalized.metadata.authenticated_owner_self_chat_target, false);
  assert.equal(normalized.metadata.from_me_classification, 'UNKNOWN_FROM_ME');
  assert.equal(normalized.metadata.final_from_me_classification, 'UNKNOWN_FROM_ME');
});

test('an unverified session cannot promote an exact self-chat to Owner Command', async (t) => {
  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape(),
    { info: { wid: OWNER } },
    ledgerFixture(t),
    null,
  );

  assert.equal(classified.classification, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(classified.authenticatedOwnerSelfChat, false);
  assert.equal(normalized.event_origin, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(normalized.owner_authenticated, false);
});

test('inverse LID to PN owner shape is an exact authenticated self-chat', () => {
  const message = ownerSelfChatShape({
    id: { _serialized: 'owner-self-chat-lid-pn', remote: OWNER },
    from: OWNER_LID,
    to: OWNER,
  });

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), true);
});

test('LID peer fails closed when only the authenticated WID and PN are known', () => {
  const pnOnlyIdentity = {
    ...AUTHENTICATED_OWNER,
    lid: null,
    aliases: [OWNER],
  };

  assert.equal(
    isAuthenticatedOwnerSelfChatMessage(ownerPnToLidSelfChatShape(), pnOnlyIdentity),
    false,
  );
});

test('external LID and PN peers never authenticate as owner self-chat', () => {
  assert.equal(isAuthenticatedOwnerSelfChatMessage(realCanaryShape(), AUTHENTICATED_OWNER), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerSelfChatShape({
      id: { _serialized: 'external-pn', remote: EXTERNAL_PN },
      to: EXTERNAL_PN,
    }),
    AUTHENTICATED_OWNER,
  ), false);
});

test('non-fromMe and missing authenticated aliases fail closed', () => {
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ fromMe: false }),
    AUTHENTICATED_OWNER,
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: [] },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: OWNER },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: [OWNER, OWNER_LID, EXTERNAL_PN] },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(ownerPnToLidSelfChatShape(), null), false);
});

test('owner verification flag and exact alias structure fail closed independently', () => {
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, ownerVerified: false },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: [OWNER_LID] },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: [OWNER, OWNER_LID, EXTERNAL_PN] },
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape(),
    { ...AUTHENTICATED_OWNER, aliases: 'malformed' },
  ), false);
});

test('all present self-chat endpoints must be exact authenticated aliases', () => {
  const mismatchedFrom = ownerPnToLidSelfChatShape({ from: EXTERNAL_PN });
  const mismatchedRemote = ownerPnToLidSelfChatShape({
    id: { _serialized: 'external-remote', remote: CONTACT_A_LID },
  });

  assert.equal(isAuthenticatedOwnerSelfChatMessage(mismatchedFrom, AUTHENTICATED_OWNER), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(mismatchedRemote, AUTHENTICATED_OWNER), false);
});

test('present but non-serializable self-chat endpoints fail closed', () => {
  const invalidEndpoint = { serialized: EXTERNAL_PN };
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ from: invalidEndpoint }),
    AUTHENTICATED_OWNER,
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ to: invalidEndpoint }),
    AUTHENTICATED_OWNER,
  ), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({
      id: { _serialized: 'invalid-remote', remote: invalidEndpoint },
    }),
    AUTHENTICATED_OWNER,
  ), false);
});

test('present empty or unknown self-chat endpoints fail closed', () => {
  for (const value of ['', '   ', 'unknown-endpoint']) {
    assert.equal(isAuthenticatedOwnerSelfChatMessage(
      ownerPnToLidSelfChatShape({ from: value }),
      AUTHENTICATED_OWNER,
    ), false);
    assert.equal(isAuthenticatedOwnerSelfChatMessage(
      ownerPnToLidSelfChatShape({ to: value }),
      AUTHENTICATED_OWNER,
    ), false);
    assert.equal(isAuthenticatedOwnerSelfChatMessage(
      ownerPnToLidSelfChatShape({ id: { _serialized: 'invalid-remote', remote: value } }),
      AUTHENTICATED_OWNER,
    ), false);
  }
});

test('truly absent optional endpoints retain safe self-chat semantics', () => {
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ from: undefined }),
    AUTHENTICATED_OWNER,
  ), true);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ to: undefined }),
    AUTHENTICATED_OWNER,
  ), true);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(
    ownerPnToLidSelfChatShape({ id: { _serialized: 'no-remote' } }),
    AUTHENTICATED_OWNER,
  ), true);
});

test('authority invalidated during provenance classification cannot become Owner Command', async () => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER }, pupPage: authorityPage() };
  const token = authorityState.beginVerification(client);
  assert.equal(authorityState.publish(token, client, AUTHENTICATED_OWNER), true);
  assert.equal(authorityState.markReadyConnected(token, client), true);
  const snapshot = authorityState.snapshot();
  let resolveClassification;
  const outboundProvenance = {
    classify: () => new Promise((resolve) => { resolveClassification = resolve; }),
  };
  const pending = classifyFinalFromMeMessage(
    ownerPnToLidSelfChatShape(),
    client,
    outboundProvenance,
    snapshot.identity,
    { isCurrent: () => authorityState.isSnapshotCurrent(snapshot) },
  );

  authorityState.invalidate();
  resolveClassification({
    classification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
    reason: 'NO_ROUTER_OUTBOUND_MATCH',
    provenance: null,
  });
  const result = await pending;

  assert.equal(result.classification, 'UNKNOWN_FROM_ME');
  assert.equal(result.reason, 'AUTHORITY_GENERATION_STALE');
  assert.equal(result.authenticatedOwnerSelfChat, false);
});

test('normalizer rejects Owner Command when authority changes after classification', async (t) => {
  const authorityState = createAuthenticatedSelfAuthorityState();
  const client = { info: { wid: OWNER }, pupPage: authorityPage() };
  const token = authorityState.beginVerification(client);
  assert.equal(authorityState.publish(token, client, AUTHENTICATED_OWNER), true);
  assert.equal(authorityState.markReadyConnected(token, client), true);
  const snapshot = authorityState.snapshot();
  const message = ownerPnToLidSelfChatShape();
  const classified = await classifyFinalFromMeMessage(
    message,
    client,
    ledgerFixture(t),
    snapshot.identity,
    { isCurrent: () => authorityState.isSnapshotCurrent(snapshot) },
  );
  assert.equal(classified.classification, 'OWNER_COMMAND');
  message.__finalFromMeClassification = classified.classification;
  message.__fromMeClassification = classified.classification;
  message.__ownerSelfChat = true;
  authorityState.invalidate();

  const normalized = normalizeInboundMessage(message, {
    clientInfo: client.info,
    authenticatedSelfIdentity: snapshot.identity,
    authenticatedSelfAuthorityCurrent: authorityState.isSnapshotCurrent(snapshot),
  });

  assert.equal(normalized.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(normalized.owner_authenticated, false);
  assert.equal(normalized.metadata.owner_self_chat, false);
  assert.equal(normalized.metadata.final_from_me_classification, 'UNKNOWN_FROM_ME');
});

for (const status of ['SENDING', 'SENT']) {
  test(`${status} Router provenance suppresses automated self-chat echo`, async (t) => {
    const ledger = ledgerFixture(t);
    const begun = ledger.begin({
      idempotency_key: `owner-control:${status.toLowerCase()}`,
      destination: OWNER,
      text: 'Espera alterada para 60 segundos.',
    });
    if (status === 'SENT') ledger.markSent(begun.record, `sent-${status.toLowerCase()}`);
    const message = ownerPnToLidSelfChatShape({
      id: { _serialized: `observed-${status.toLowerCase()}`, remote: OWNER_LID },
      body: 'Espera alterada para 60 segundos.',
    });

    const { classified, normalized } = await normalizeAfterFinalClassification(
      message,
      { info: { wid: OWNER } },
      ledger,
    );

    assert.equal(classified.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
    assert.equal(normalized.event_origin, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
    assert.equal(normalized.owner_authenticated, false);
    assert.equal(normalized.metadata.owner_self_chat, false);
    assert.equal(normalized.metadata.authenticated_owner_self_chat_target, true);
  });
}

test('replayed automated self-chat observation remains suppressed', async (t) => {
  const ledger = ledgerFixture(t);
  ledger.begin({
    idempotency_key: 'owner-control:replayed-echo',
    destination: OWNER,
    text: 'Espera desativada.',
  });
  const client = { info: { wid: OWNER } };
  const first = ownerPnToLidSelfChatShape({
    id: { _serialized: 'echo-replay-reference', remote: OWNER_LID },
    body: 'Espera desativada.',
  });
  const replay = ownerPnToLidSelfChatShape({
    id: { _serialized: 'echo-replay-reference', remote: OWNER_LID },
    body: 'Espera desativada.',
  });

  const firstResult = await normalizeAfterFinalClassification(first, client, ledger);
  const replayResult = await normalizeAfterFinalClassification(replay, client, ledger);
  assert.equal(firstResult.classified.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
  assert.equal(replayResult.classified.classification, 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED');
  assert.equal(replayResult.normalized.owner_authenticated, false);
});

test('FAILED fingerprint provenance can never become an Owner Command', async (t) => {
  const ledger = ledgerFixture(t);
  const begun = ledger.begin({
    idempotency_key: 'owner-control:failed-fingerprint',
    destination: OWNER,
    text: 'Possibly delivered confirmation.',
  });
  ledger.markFailed(begun.record, 'TransportError');
  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape({ body: 'Possibly delivered confirmation.' }),
    { info: { wid: OWNER } },
    ledger,
  );

  assert.equal(classified.classification, 'UNKNOWN_FROM_ME');
  assert.equal(normalized.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(normalized.owner_authenticated, false);
});

test('FAILED exact provenance can never become an Owner Command', async (t) => {
  const ledger = ledgerFixture(t);
  const begun = ledger.begin({
    idempotency_key: 'owner-control:failed-exact',
    destination: OWNER,
    text: 'Possibly delivered exact confirmation.',
  });
  const sent = ledger.markSent(begun.record, 'failed-exact-reference');
  ledger.markFailed(sent, 'TransportError');
  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape({
      id: { _serialized: 'failed-exact-reference', remote: OWNER_LID },
      body: 'Possibly delivered exact confirmation.',
    }),
    { info: { wid: OWNER } },
    ledger,
  );

  assert.equal(classified.classification, 'UNKNOWN_FROM_ME');
  assert.equal(classified.reason, 'FAILED_OUTBOUND_PROVENANCE_MATCH');
  assert.equal(normalized.owner_authenticated, false);
});

test('multiple owner self-chat provenance matches fail closed', async (t) => {
  const ledger = ledgerFixture(t);
  for (const suffix of ['a', 'b']) {
    ledger.begin({
      idempotency_key: `owner-control:multiple:${suffix}`,
      destination: OWNER,
      text: 'Repeated confirmation.',
    });
  }
  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape({ body: 'Repeated confirmation.' }),
    { info: { wid: OWNER } },
    ledger,
  );

  assert.equal(classified.classification, 'UNKNOWN_FROM_ME');
  assert.equal(classified.reason, 'MULTIPLE_OUTBOUND_PROVENANCE_MATCHES');
  assert.equal(normalized.owner_authenticated, false);
});

test('stale owner self-chat provenance fails closed', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'attention-self-chat-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let now = new Date('2026-09-04T12:00:00Z');
  const ledger = createOutboundProvenanceLedger(
    { outboundProvenanceDir: directory, outboundProvenanceCorrelationMs: 120000 },
    { now: () => now },
  );
  const begun = ledger.begin({
    idempotency_key: 'owner-control:stale',
    destination: OWNER,
    text: 'Old confirmation.',
  });
  ledger.markSent(begun.record, 'old-reference');
  now = new Date('2026-09-04T12:02:01Z');

  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape({ body: 'Old confirmation.' }),
    { info: { wid: OWNER } },
    ledger,
  );
  assert.equal(classified.classification, 'UNKNOWN_FROM_ME');
  assert.equal(classified.reason, 'STALE_OUTBOUND_PROVENANCE_MATCH');
  assert.equal(normalized.owner_authenticated, false);
});

test('unavailable provenance fails closed for owner self-chat', async () => {
  const { classified, normalized } = await normalizeAfterFinalClassification(
    ownerPnToLidSelfChatShape(),
    { info: { wid: OWNER } },
    null,
  );
  assert.equal(classified.classification, 'UNKNOWN_FROM_ME');
  assert.equal(classified.reason, 'OUTBOUND_PROVENANCE_UNAVAILABLE');
  assert.equal(normalized.owner_authenticated, false);
});

test('authenticated owner sender alone never proves self-chat', () => {
  const message = {
    fromMe: true,
    from: OWNER,
    to: 'third-party@c.us',
  };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), false);
});

test('id.remote is the directional peer fallback when message.to is absent', () => {
  const thirdParty = {
    id: { remote: CONTACT_A_LID },
    fromMe: true,
    from: OWNER,
  };
  const selfChat = {
    id: { remote: OWNER },
    fromMe: true,
    from: OWNER,
  };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(thirdParty, AUTHENTICATED_OWNER), false);
  assert.equal(isAuthenticatedOwnerSelfChatMessage(selfChat, AUTHENTICATED_OWNER), true);
});

test('ambiguous outbound peer fails closed for Owner Command authority', () => {
  const message = {
    fromMe: true,
    from: OWNER,
    chatId: OWNER,
  };

  assert.equal(isAuthenticatedOwnerSelfChatMessage(message, AUTHENTICATED_OWNER), false);
});
