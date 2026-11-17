const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createInboundBridge } = require('../src/bridge');
const { canaryConfig } = require('../src/semantic-canary');
const { drainInboundPending, listPending } = require('../src/spool');
const { captureInboundMedia } = require('../src/media');

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'synthetic-canary-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = { tenantId: 'synthetic-tenant', sourceAccount: 'synthetic',
    inboundSource: 'wwebjs', inboundChannel: 'whatsapp', inboundForwardEnabled: true,
    inboundForwardUrl: 'http://localhost/events', inboundForwardHosts: ['localhost'],
    hmacSecret: 'synthetic-canary-hmac-secret-32-bytes', inboundHttpTimeoutMs: 200,
    mediaRoot: path.join(root, 'media'), mediaMaxBytes: 1000, mediaDownloadTimeoutMs: 1000,
    mediaNotificationUrl: 'http://localhost/media', conversationCaptureOwnerVoiceEnabled: true,
    conversationCaptureOwnerTextEnabled: true, semanticCanaryEnabled: true,
    semanticCanaryConversationKey: 'wwebjs:1000000000002@lid',
    semanticCanaryIngressUrl: 'http://localhost/api/v1/ingress/internal/semantic-canary' };
  for (const key of ['inboundSpoolDir', 'inboundPendingDir', 'inboundSendingDir', 'inboundSentDir',
    'inboundQuarantineDir', 'mediaNotificationPendingDir', 'mediaNotificationSendingDir',
    'mediaNotificationSentDir', 'mediaNotificationQuarantineDir']) config[key] = path.join(root, key);
  return config;
}
const logger = { info() {}, warn() {}, error() {} };
function message(peer = '1000000000002@lid') {
  return { id: { _serialized: 'synthetic-'+peer, remote: peer, fromMe: true },
    from: '5500000000001@c.us', to: peer, fromMe: true, type: 'ptt', hasMedia: true,
    timestamp: Math.floor(Date.now()/1000),
    __fromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED',
    __finalFromMeClassification: 'OWNER_MANUAL_OUTBOUND_OBSERVED' };
}
function response(allowed = true) {
  return { status: 202, text: async () => JSON.stringify({status:'accepted',
    canary_media_allowed:allowed, grants_authority:false}) };
}

test('selected media revalidates policy and keeps source IDs; outside owner audio is blocked', async t => {
  const config = fixture(t); const urls = []; let downloads = 0;
  const bridge = createInboundBridge(config, logger, { fetchImpl: async url => { urls.push(url); return response(); } });
  const source = message();
  source.downloadMedia = async () => { downloads++; return {mimetype:'audio/ogg',data:Buffer.from('synthetic').toString('base64')}; };
  const result = await bridge.handleMessage(source);
  assert.equal(downloads, 1);
  assert.equal(result.normalized.event_origin, 'OWNER_MANUAL_OUTBOUND_OBSERVED');
  assert.equal(result.normalized.owner_authenticated, false);
  assert.ok(urls.every(url => url.includes('/semantic-canary/')));
  await bridge.handleMessage(source);
  assert.equal(downloads, 1); // persisted media receipt, even after replay
  const outside = message('1000000000003@lid');
  outside.downloadMedia = source.downloadMedia;
  await bridge.handleMessage(outside);
  assert.equal(downloads, 1);
  assert.equal(urls.at(-1), config.inboundForwardUrl);
});

test('denied policy never downloads, and disabling cannot replay through normal ingress', async t => {
  const config = fixture(t); let downloads = 0;
  const source = message(); source.downloadMedia = async () => { downloads++; };
  const denied = createInboundBridge(config, logger, { fetchImpl: async () => response(false) });
  assert.equal((await denied.handleMessage(source)).media.error_code, 'CANARY_MEDIA_DENIED');
  assert.equal(downloads, 0);
  const failed = createInboundBridge(config, logger, { fetchImpl: async () => ({status:503,text:async()=>''}) });
  await failed.handleMessage(source);
  const selected = canaryConfig(config);
  assert.equal(listPending(selected).length, 1);
  config.semanticCanaryEnabled = false;
  let calls = 0;
  await drainInboundPending(config, logger, async () => { calls++; return response(); });
  const stopped = createInboundBridge(config, logger, { fetchImpl: async () => { calls++; return response(); } });
  assert.equal((await stopped.handleMessage(source)).status, 'canary_disabled');
  assert.equal(calls, 0);
  assert.equal(listPending(selected).length, 1);
  assert.equal(downloads, 0);
});

test('revocation during download discards bytes before persistence and does not retry', async t => {
  const config = canaryConfig(fixture(t)); let allowed = true; let downloads = 0; let notices = 0;
  const source = message();
  source.downloadMedia = async () => { downloads++; allowed = false;
    return {mimetype:'audio/ogg',data:Buffer.from('synthetic-revoked').toString('base64')}; };
  const event = {tenant_id:'synthetic-tenant', source:'wwebjs', external_event_id:'synthetic',
    message_type:'ptt', metadata:{source_account:'synthetic'}, identity:{idempotency_key:'synthetic'}};
  const result = await captureInboundMedia(config, source, event, logger,
    async () => { notices++; return response(); }, async () => allowed);
  assert.equal(result.error_code, 'CANARY_MEDIA_DENIED');
  assert.equal(downloads, 1); assert.equal(notices, 0);
  assert.deepEqual(fs.readdirSync(config.mediaRoot), ['.capture-receipts']);
  assert.equal((await captureInboundMedia(config, source, event, logger,
    async () => response(), async () => allowed)).error_code, 'CANARY_MEDIA_DENIED');
  assert.equal(downloads, 1);
});

test('invalid scope prevents opt-in; concurrent First Contact requires a canonical LID', t => {
  const config = fixture(t);
  for (const change of [{semanticCanaryConversationKey:'wwebjs:synthetic@g.us'},
    {semanticCanaryIngressUrl:'http://example.com/canary'}, {sourceAccount:''}, {firstContactEnabled:true, semanticCanaryConversationKey:'wwebjs:5500000000002@c.us'}]) {
    assert.throws(() => createInboundBridge({...config,...change}, logger), /CANARY_/);
  }
  assert.equal(canaryConfig({}), null);
  assert.doesNotThrow(() => createInboundBridge({...config, firstContactEnabled:true}, logger));
});


test('revoked notification is terminal and never reported as accepted media', async t => {
  const config = canaryConfig(fixture(t)); let downloads = 0;
  const source = message();
  source.downloadMedia = async () => { downloads++;
    return {mimetype:'audio/ogg', data:Buffer.from('synthetic').toString('base64')}; };
  const event = {tenant_id:'synthetic-tenant', source:'wwebjs', external_event_id:'synthetic',
    message_type:'ptt', metadata:{source_account:'synthetic'}, identity:{idempotency_key:'synthetic'}};
  const denied = async () => ({status:202, text:async()=>JSON.stringify({status:'canary_blocked'})});
  for (let i=0;i<2;i++) {
    const result = await captureInboundMedia(config, source, event, logger, denied, async()=>true);
    assert.equal(result.status, 'media_notification_denied');
  }
  assert.equal(downloads, 1);
});
