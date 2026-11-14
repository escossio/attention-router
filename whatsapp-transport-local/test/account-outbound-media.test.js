const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const { createInboundBridge } = require('../src/bridge');
const { createConfig } = require('../src/config');
const { identityFromAliases } = require('../src/conversation');
const { listPending } = require('../src/spool');

const TENANT = '00000000-0000-4000-8000-000000000001';
const OWNER = '5500000000001@c.us';
const OWNER_LID = '123456789@lid';
const PEER = '23456789@lid';
const identity = {
  ownerVerified: true,
  pn: OWNER,
  lid: OWNER_LID,
  wid: OWNER,
  aliases: [OWNER, OWNER_LID],
};
const options = {
  authenticatedSelfAuthorityCurrent: true,
  authenticatedSelfIdentity: identity,
  clientInfo: { wid: OWNER },
};
const logger = { info() {}, warn() {}, error() {} };

function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'andy-account-outbound-media-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const spool = path.join(root, 'spool');
  return {
    ...createConfig({
      LOCAL_TENANT_ID: TENANT,
      INBOUND_FORWARD_ENABLED: 'true',
      WHATSAPP_ARTIFACT_INGESTION_ENABLED: 'true',
      CONVERSATION_CAPTURE_UNATTRIBUTED_OUTBOUND_MEDIA_ENABLED: 'true',
      LOCAL_INBOUND_FORWARD_URL: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
      LOCAL_MEDIA_NOTIFICATION_URL: 'http://127.0.0.1:18102/internal/whatsapp/media',
      LOCAL_INBOUND_SOURCE: 'wwebjs',
      LOCAL_INBOUND_CHANNEL: 'whatsapp',
      INTERNAL_INGRESS_HMAC_SECRET: 'synthetic-secret-do-not-use-in-prod-1234567',
      LOCAL_INBOUND_SPOOL_DIR: spool,
      LOCAL_INBOUND_PENDING_DIR: path.join(spool, 'pending'),
      LOCAL_INBOUND_SENDING_DIR: path.join(spool, 'sending'),
      LOCAL_INBOUND_SENT_DIR: path.join(spool, 'sent'),
      LOCAL_INBOUND_QUARANTINE_DIR: path.join(spool, 'quarantine'),
      LOCAL_MEDIA_NOTIFICATION_PENDING_DIR: path.join(spool, 'media-notifications', 'pending'),
      LOCAL_MEDIA_NOTIFICATION_SENDING_DIR: path.join(spool, 'media-notifications', 'sending'),
      LOCAL_MEDIA_NOTIFICATION_SENT_DIR: path.join(spool, 'media-notifications', 'sent'),
      LOCAL_MEDIA_NOTIFICATION_QUARANTINE_DIR: path.join(spool, 'media-notifications', 'quarantine'),
      WHATSAPP_MEDIA_ROOT: path.join(root, 'media'),
      ...overrides,
    }),
  };
}

function imageMessage(overrides = {}) {
  const bytes = Buffer.from('SYNTHETIC IMAGE BYTES, NO REAL PERSONAL DATA');
  return {
    id: {
      _serialized: 'true_' + PEER + '_synthetic_photo_1',
      remote: PEER,
      fromMe: true,
    },
    from: OWNER,
    to: PEER,
    fromMe: true,
    type: 'image',
    body: 'SENSITIVE TEXT MUST NOT BE RECORDED AS HUMAN MESSAGE',
    hasMedia: true,
    timestamp: 1794693600,
    __fromMeClassification: 'UNKNOWN_FROM_ME',
    __finalFromMeClassification: 'UNKNOWN_FROM_ME',
    __fromMeClassificationReason: 'UNMATCHED_MEDIA_FROM_ME',
    __outboundProvenance: null,
    __conversationIdentity: identityFromAliases([PEER]),
    downloadMedia: async () => ({
      mimetype: 'image/png',
      data: bytes.toString('base64'),
    }),
    ...overrides,
  };
}

function responder(log) {
  return async (url, req) => {
    const payload = JSON.parse(Buffer.from(req.body).toString('utf8'));
    log.push({ url, payload });
    return { status: 202, text: async () => '{"status":"accepted"}' };
  };
}

test('owner-sent image is recorded as unattributed account evidence with exact signed receipt', async (t) => {
  const cfg = fixture(t);
  assert.equal(cfg.conversationCaptureUnattributedOutboundMediaEnabled, true);
  const log = [];
  let downloads = 0;
  const msg = imageMessage({ downloadMedia: async () => {
    downloads += 1;
    return { mimetype: 'image/png', data: Buffer.from('SYNTHETIC IMAGE').toString('base64') };
  } });
  const bridge = createInboundBridge(cfg, logger, { fetchImpl: responder(log) });
  const output = await bridge.handleMessage(msg, options);
  assert.equal(output.status, 'delivered');
  assert.equal(downloads, 1);
  assert.equal(output.media.status, 'media_ready_notified');
  assert.equal(log.length, 2);
  const event = log.find(x => x.url === cfg.inboundForwardUrl)?.payload;
  const media = log.find(x => x.url === cfg.mediaNotificationUrl)?.payload;
  assert.ok(event && media);
  assert.equal(event.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(event.owner_authenticated, false);
  assert.equal(event.metadata.from_me, true);
  assert.equal(event.metadata.from_me_classification, 'UNKNOWN_FROM_ME');
  assert.equal(event.metadata.final_from_me_classification, 'UNKNOWN_FROM_ME');
  assert.equal(event.metadata.from_me_classification_reason, 'UNMATCHED_MEDIA_FROM_ME');
  assert.equal(event.metadata.account_outbound_media_capture_basis, 'VERIFIED_ACCOUNT_UNATTRIBUTED');
  assert.equal(event.metadata.outbound_provenance, null);
  assert.equal(event.content, '');
  assert.equal(event.metadata.conversation_state, 'READY');
  assert.equal(media.tenant_id, event.tenant_id);
  assert.equal(media.external_event_id, event.external_event_id);
  assert.equal(media.capture_status, 'READY');
  assert.equal(media.mime_type, 'image/png');
  assert.equal(media.content_sha256, crypto.createHash('sha256').update('SYNTHETIC IMAGE').digest('hex'));
  assert.equal(listPending(cfg).length, 0);
});

test('unmatched media reference is accepted as unattributed, not claimed manual', async (t) => {
  const cfg = fixture(t);
  const log = [];
  const output = await createInboundBridge(cfg, logger, { fetchImpl: responder(log) })
    .handleMessage(imageMessage({ type: 'document', __fromMeClassificationReason: 'UNMATCHED_MEDIA_REFERENCE' }), options);
  assert.equal(output.status, 'delivered');
  assert.equal(output.media.status, 'media_ready_notified');
  const event = log.find(x => x.url === cfg.inboundForwardUrl).payload;
  assert.equal(event.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(event.message_type, 'document');
  assert.equal(event.content, '');
  assert.equal(event.metadata.from_me_classification_reason, 'UNMATCHED_MEDIA_REFERENCE');
});

test('fail-closed matrix never downloads an untrusted outbound item', async (t) => {
  const cfg = fixture(t);
  let downloads = 0;
  const base = imageMessage({
    downloadMedia: async () => {
      downloads++;
      return { mimetype: 'image/png', data: Buffer.from('test').toString('base64') };
    },
  });
  const wrong = [
    [{}, { ...cfg, conversationCaptureUnattributedOutboundMediaEnabled: false }],
    [{}, { ...cfg, whatsappArtifactIngestionEnabled: false }],
    [{}, { ...cfg, mediaNotificationUrl: 'https://example.invalid/upload' }],
    [{}, { ...cfg, hmacSecret: '' }],
    [{}, { ...cfg, inboundForwardEnabled: false }],
    [{}, { ...cfg, tenantId: null }],
    [{ type: 'ptt' }, cfg],
    [{ type: 'video' }, cfg],
    [{ type: 'sticker' }, cfg],
    [{ hasMedia: false }, cfg],
    [{ __fromMeClassification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED',
       __finalFromMeClassification: 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED' }, cfg],
    [{ __fromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
       __finalFromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED' }, cfg],
    [{ __fromMeClassificationReason: 'OUTBOUND_PROVENANCE_UNAVAILABLE' }, cfg],
    [{ __fromMeClassificationReason: 'CONVERSATION_IDENTITY_AMBIGUOUS' }, cfg],
    [{ __fromMeClassificationReason: 'MULTIPLE_OUTBOUND_PROVENANCE_MATCHES' }, cfg],
    [{ __fromMeClassificationReason: 'MEDIA_REFERENCE_PENDING' }, cfg],
    [{ __fromMeClassificationReason: 'AUTHORITY_GENERATION_STALE' }, cfg],
    [{ __fromMeClassificationReason: null }, cfg],
    [{ __ownerSelfChat: true }, cfg],
    [{ __outboundProvenance: { outbox_id: 'synthetic-outbox' } }, cfg],
    [{ from: '5500000000999@c.us' }, cfg],
    [{ id: { ...base.id, fromMe: false } }, cfg],
    [{ id: { ...base.id, remote: 'somegroup@g.us' } }, cfg],
    [{ __conversationIdentity: { state: 'AMBIGUOUS', peer_id: null, peer_identifiers: [] } }, cfg],
  ];
  for (const [patch, config] of wrong) {
    const logs = [];
    const result = await createInboundBridge(config, logger, { fetchImpl: responder(logs) })
      .handleMessage({ ...base, ...patch }, options);
    assert.ok(result.media == null, JSON.stringify(Object.keys(patch)));
  }
  const extras = [
    { ...options, authenticatedSelfAuthorityCurrent: false },
    { ...options, authenticatedSelfIdentity: null },
    { ...options, clientInfo: { wid: '5500000000999@c.us' } },
    { ...options, authenticatedSelfIdentity: { ...identity, ownerVerified: false } },
    { ...options, authenticatedSelfIdentity: { ...identity, aliases: [OWNER] } },
  ];
  for (const scoped of extras) {
    const result = await createInboundBridge(cfg, logger, { fetchImpl: responder([]) })
      .handleMessage(base, scoped);
    assert.ok(result.media == null);
  }
  assert.equal(downloads, 0);
});

test('opt-in defaults off and requires tenant, signed forwarding, Artifact plane', () => {
  assert.equal(createConfig({}).conversationCaptureUnattributedOutboundMediaEnabled, false);
  const settings = {
    CONVERSATION_CAPTURE_UNATTRIBUTED_OUTBOUND_MEDIA_ENABLED: 'true',
    INBOUND_FORWARD_ENABLED: 'true',
    WHATSAPP_ARTIFACT_INGESTION_ENABLED: 'true',
    LOCAL_TENANT_ID: TENANT,
    LOCAL_INBOUND_FORWARD_URL: 'http://127.0.0.1:18102/api/v1/ingress/internal/events',
    INTERNAL_INGRESS_HMAC_SECRET: 'synthetic-secret-do-not-use-in-prod-1234567',
  };
  assert.equal(createConfig(settings).conversationCaptureUnattributedOutboundMediaEnabled, true);
  for (const key of [
    'INBOUND_FORWARD_ENABLED',
    'WHATSAPP_ARTIFACT_INGESTION_ENABLED',
    'LOCAL_TENANT_ID',
    'LOCAL_INBOUND_FORWARD_URL',
    'INTERNAL_INGRESS_HMAC_SECRET',
  ]) {
    assert.equal(createConfig({ ...settings, [key]: '' }).conversationCaptureUnattributedOutboundMediaEnabled, false);
  }
  assert.equal(createConfig({ ...settings, LOCAL_INBOUND_SOURCE: 'gmail' }).conversationCaptureUnattributedOutboundMediaEnabled, false);
  assert.equal(createConfig({ ...settings, INTERNAL_INGRESS_HMAC_SECRET: 'short' }).conversationCaptureUnattributedOutboundMediaEnabled, false);
  assert.equal(createConfig({ ...settings, LOCAL_INBOUND_CHANNEL: 'telegram' }).conversationCaptureUnattributedOutboundMediaEnabled, false);
});

test('notification failure remains durable and keeps the unknown attribution', async (t) => {
  const cfg = fixture(t);
  const events = [];
  const failure = await createInboundBridge(cfg, logger, { fetchImpl: responder(events) })
    .handleMessage(imageMessage({
      downloadMedia: async () => { throw new Error('DOWNLOAD_EMPTY'); },
    }), options);
  assert.equal(failure.status, 'delivered');
  assert.equal(failure.media.status, 'media_capture_failed');
  assert.equal(events.length, 2);
  const event = events.find(x => x.url === cfg.inboundForwardUrl).payload;
  const media = events.find(x => x.url === cfg.mediaNotificationUrl).payload;
  assert.equal(event.event_origin, 'UNKNOWN_FROM_ME');
  assert.equal(event.owner_authenticated, false);
  assert.equal(event.content, '');
  assert.equal(media.capture_status, 'FAILED');
  assert.equal(media.media_ref, undefined);
  assert.equal(media.content_sha256, undefined);
});
